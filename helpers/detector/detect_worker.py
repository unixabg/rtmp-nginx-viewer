#!/usr/bin/env python3
"""
detect_worker.py - run object detection over a single recorded video file and
emit a JSON manifest of what was found.

This is deliberately the unit of work for ONE file. Parallelism (across cores
and across cluster nodes) is handled outside, by GNU Parallel - see
run_detection.sh. Keeping the worker single-file makes it trivially parallel:
each invocation is independent, reads one read-only input, and writes its own
output, so there is nothing to coordinate and no shared state to corrupt.

Design rules that keep this safe alongside a live 24/7 recorder:
  * The input tree is mounted READ-ONLY. This process never writes there.
  * Output (thumbnails + manifest) goes to a SEPARATE directory.
  * Frames are SAMPLED, not all decoded - you do not need 25fps to find a
    person in a 5-minute clip. One frame every FRAME_INTERVAL_SEC is plenty.
  * A cheap motion gate (frame differencing) runs before the model, so the
    expensive detector only sees frames where something actually changed.
  * If a file is still being written (recorder hasn't closed it yet) the
    caller should exclude it; we also skip very recently modified files.

The detector is pluggable. By default it uses an Ultralytics YOLO model on
CPU. To use a Coral, an Nvidia GPU (TensorRT/ONNX), or a remote inference
server, replace only the Detector class - the rest of the pipeline and the
manifest format stay identical.

The decoder is pluggable too (DECODE / DECODE_KEYFRAMES, see the tunables).
Once the model is fast - a GPU node - decode is where all the time goes.
The big saving is decoding only keyframes when the sample interval allows
it; NVDEC via ffmpeg is available but opt-in, because on a consumer card
it measured no faster than the CPU. The manifest records which decoder
produced the frames.

Performance note (from testing on a Raspberry Pi 5): YOLO11n took ~400ms per
inference in PyTorch but ~80ms exported to NCNN. Model format matters more
than raw hardware for CPU throughput; export once with
    yolo export model=yolo11n.pt format=ncnn
and point MODEL_PATH at the exported directory.

Usage:
    detect_worker.py --input /mnt/recordings/cam03/2026-07-09_14-05-00.mp4 \
                     --input-root /mnt/recordings \
                     --output-root /var/detections
Exit codes: 0 ok (including "skipped"), 1 error on this file (parallel will
log it via --joblog; the run is resumable so a retry just re-runs the file).
"""

import argparse
import fnmatch
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import cv2  # opencv-python-headless
import numpy as np

# ---------------------------------------------------------------- tunables
FRAME_INTERVAL_SEC = 2.0     # sample one frame every N seconds of video
MOTION_MIN_AREA = 1500       # px^2 of changed area required to run the model
MOTION_DOWNSCALE_W = 480     # motion gate runs on a downscaled grayscale frame
MIN_FILE_AGE_SEC = 120       # skip files modified more recently than this
CONF_THRESHOLD = 0.45
CLASSES_OF_INTEREST = {"person", "car", "truck", "bicycle", "motorcycle",
                       "bus", "dog", "cat"}
MODEL_PATH = os.environ.get("DETECT_MODEL", "yolo11n.pt")
THUMB_MAX_W = 640
# How frames are pulled off disk. Decode, not inference, is where a GPU node
# spends its time (see the timing block in any manifest), so this is the
# knob that matters once the model is fast:
#   auto   - keyframe-only decode when the interval allows it, else ffmpeg
#            on the CPU, else OpenCV. NVDEC is NOT chosen automatically:
#            measured on an RTX 3070 with 1440p H.264, one consumer NVDEC
#            shared by four workers was no faster than four CPU cores.
#   nvdec  - use hardware decode (for cameras opted into sub-GOP sampling
#            on a node where it measures faster); fail if unavailable.
#   ffmpeg - ffmpeg software decode (frame sampling happens inside ffmpeg,
#            and decode runs in its own process alongside inference).
#   cv2    - the original OpenCV grab/retrieve loop.
DECODE = os.environ.get("DECODE", "auto").lower()
# Threads per ffmpeg software decoder. 1 is right when several workers run
# in parallel (each worker = one core); raise it only for a single worker.
DECODE_THREADS = int(os.environ.get("DECODE_THREADS", "1"))
# Keyframe-only decode. When the sample interval is at least the camera's
# keyframe spacing (GOP), every sampled frame can be a keyframe, and
# "-skip_frame nokey" makes the decoder parse-and-discard the frames in
# between instead of decoding them: ~GOP-length-in-frames less work, on
# any node. Sample times snap to keyframes. "auto" probes the GOP of each
# file and uses keyframe mode when interval >= GOP * KEYFRAME_MIN_RATIO;
# "1" forces it (interval is raised to the GOP if needed); "0" disables.
# Not combined with NVDEC: decoding one frame per GOP is cheap anywhere.
KEYFRAMES = os.environ.get("DECODE_KEYFRAMES", "auto").lower()
KEYFRAME_MIN_RATIO = 0.9
GOP_PROBE_SEC = 20
# Decode frames no wider than this (0 = source resolution, the default).
# The idea: nothing downstream needs full resolution - the model runs at
# 640x640 and the motion gate at 480 wide - so scaling inside ffmpeg
# should save the BGR24 conversion and ~11 MB per frame through the pipe.
# Measured a 5x saving in standalone ffmpeg runs, but NOT in the worker:
# 862 files at source resolution averaged 9.7s against 200 files at 1280
# averaging 11.7s, a 17% regression. Frames are consumed one at a time as
# the Python side is ready, so the pipe is never the constraint and the
# swscale pass is pure added cost. Left as an option because a slower
# node (a Pi decoding 1440p) may balance differently; bench's
# decoder@width column makes it a one-tick experiment. Boxes are scaled
# back to source coordinates before the manifest either way.
DECODE_MAX_W = int(os.environ.get("DECODE_MAX_W", "0"))
# Denser sampling on GPU nodes. With inference at ~10 ms, sampling more
# often costs only the motion gate - but an interval below the camera's
# keyframe spacing gives up keyframe-only decode (below), which is a much
# bigger saving. So density is opt-in per camera: "gpu_interval" in
# cameras.json is used verbatim on CUDA nodes. INTERVAL_SCALE multiplies
# every camera's interval for the cameras without one: "auto" =
# GPU_INTERVAL_SCALE on CUDA nodes (default 1.0, i.e. no change), 1.0
# elsewhere; or a number to force it anywhere.
INTERVAL_SCALE = os.environ.get("INTERVAL_SCALE", "auto").lower()
GPU_INTERVAL_SCALE = float(os.environ.get("GPU_INTERVAL_SCALE", "1.0"))
MIN_INTERVAL_SEC = 0.1

# ------------------------------------------------------------- tracking
# Per-frame detections are grouped into TRACKS so one object seen across
# many sampled frames is one event, not dozens. Association is IoU-based
# against the previous sighting: same label + overlapping box = same object.
TRACK_IOU = 0.3          # min overlap to call it the same object
# A track closes after this many consecutive INFERRED frames without a match.
# Counting observations rather than seconds matters because the motion gate
# can suppress inference for minutes: gated frames are not evidence that an
# object left, so they must not age a track out.
TRACK_MAX_GAP_OBS = 3
TRACK_MAX_GAP_SEC = 600.0  # hard cap, so unrelated objects never merge
# In a still scene the gate suppresses everything, so a parked car is seen
# once and can't be judged stationary (one position shows no displacement).
# Run inference anyway this often, so persistent objects keep being observed.
# 0 disables. Cost is bounded: video_length / keepalive extra inferences.
KEEPALIVE_SEC = 30.0
# With sampled frames, a fast object can move further than its own width
# between samples, so boxes don't overlap and IoU alone fragments the track.
# Fall back to a centre-distance gate, measured against the position
# PREDICTED from the track's recent velocity, in units of box diagonal.
TRACK_MAX_MOVE = 1.2
# "Stationary" = the box centre never moved more than this fraction of the
# box's own size over the track's life. Parked cars, furniture, etc.
STATIONARY_FRAC = 0.25

# Installed version, written by `make install`/`upgrade` from git describe.
# Stamped into every manifest so a support question can start from "what
# were you running" instead of a file diff.
def _version() -> str:
    try:
        return (Path(__file__).resolve().parent / "VERSION").read_text().strip() or "unknown"
    except OSError:
        return "unknown"


VERSION = _version()

# Per-camera rules file. Defaults to cameras.json next to this script;
# override with CAMERA_RULES=/path/to/file.json
RULES_PATH = os.environ.get(
    "CAMERA_RULES", str(Path(__file__).resolve().parent / "cameras.json"))


def load_rules(path: str) -> dict:
    """Load per-camera rules. Missing/invalid file -> built-in defaults.

    Never fatal: a typo in the config should degrade to default behavior
    with a warning, not stop a 10,000-file backfill.
    """
    fallback = {"rules": [], "default": {
        "classes": sorted(CLASSES_OF_INTEREST),
        "conf": CONF_THRESHOLD,
        "min_area": MOTION_MIN_AREA,
        "interval": FRAME_INTERVAL_SEC}}
    try:
        with open(path) as fh:
            cfg = json.load(fh)
    except FileNotFoundError:
        return fallback
    except (json.JSONDecodeError, OSError) as e:
        print(f"WARNING: bad rules file {path}: {e}; using defaults",
              file=sys.stderr)
        return fallback
    cfg.setdefault("rules", [])
    cfg.setdefault("default", fallback["default"])
    return cfg


def rules_for(filename: str, cfg: dict) -> dict:
    """First matching rule wins; fall back to 'default'.

    Matching is fnmatch (shell glob) against the file's BASENAME, which is
    where camera identity lives in nginx-rtmp's naming, e.g.
    'Camera32-1788648910-20260905-175510.mp4' matches 'Camera3?-*'.
    """
    for rule in cfg["rules"]:
        pat = rule.get("pattern")
        if pat and fnmatch.fnmatch(filename, pat):
            merged = dict(cfg["default"])
            merged.update(rule)
            return merged
    return dict(cfg["default"])


_cuda = None


def has_cuda() -> bool:
    """CUDA visible to torch? Cached; torch is imported by the detector
    anyway so this costs nothing extra on a CPU node beyond the import."""
    global _cuda
    if _cuda is None:
        try:
            import torch
            _cuda = bool(torch.cuda.is_available())
        except Exception:  # noqa: BLE001
            _cuda = False
    return _cuda


def effective_interval(rule: dict, cfg: dict):
    """(interval_sec, how) for this file on this node.

    how is a short string for the manifest: 'rule', 'gpu_interval',
    'scale=0.25', so a reader can tell why two nodes sampled a camera
    differently."""
    base = float(rule.get("interval", cfg.get("default", {})
                          .get("interval", FRAME_INTERVAL_SEC)))
    gpu = has_cuda()
    if gpu and "gpu_interval" in rule:
        return max(float(rule["gpu_interval"]), MIN_INTERVAL_SEC), "gpu_interval"
    if INTERVAL_SCALE == "auto":
        scale = GPU_INTERVAL_SCALE if gpu else 1.0
    else:
        scale = float(INTERVAL_SCALE)
    if scale == 1.0:
        return base, "rule"
    return max(base * scale, MIN_INTERVAL_SEC), f"scale={scale:g}"


# ------------------------------------------------------------- decoding
# A frame source yields (timestamp_sec, frame_bgr) for every SAMPLED frame
# and keeps its own decode-time tally in .t_decode. process() does not care
# which one it got. Three implementations, best first:
#
#   NvdecSource  ffmpeg + <codec>_cuvid: the card's fixed-function decoder
#                does the H.264/HEVC work, the CPU only receives frames.
#   FfmpegSource ffmpeg software decode. Still better than cv2 because the
#                fps filter drops unsampled frames before they cross the
#                pipe, and decode overlaps inference in a second process.
#   CvSource     the original OpenCV grab()/retrieve() loop. No ffmpeg
#                binary needed; opencv-python-headless bundles its own.
#
# Explicit cuvid decoders are used rather than "-hwaccel cuda" because the
# latter silently falls back to software when the hardware path fails, and
# then the manifest would claim NVDEC for a CPU-decoded file.

_CUVID = {"h264": "h264_cuvid", "hevc": "hevc_cuvid", "h265": "hevc_cuvid",
          "mpeg4": "mpeg4_cuvid", "mpeg2video": "mpeg2_cuvid",
          "vp8": "vp8_cuvid", "vp9": "vp9_cuvid", "mjpeg": "mjpeg_cuvid",
          "av1": "av1_cuvid"}


def _ffprobe(path: Path) -> dict:
    """width/height/codec/duration of the first video stream, via ffprobe."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,codec_name,r_frame_rate"
                          ":format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {out.stderr.strip()[:200]}")
    d = json.loads(out.stdout)
    st = (d.get("streams") or [{}])[0]
    num, _, den = (st.get("r_frame_rate") or "25/1").partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 25.0
    return {"w": int(st["width"]), "h": int(st["height"]),
            "codec": st.get("codec_name", ""),
            "fps": fps or 25.0,
            "duration": float(d.get("format", {}).get("duration") or 0)}


def _probe_gop(path: Path) -> float:
    """Median keyframe spacing in seconds over the first GOP_PROBE_SEC of
    the file, or 0 if it cannot be determined (fewer than two keyframes)."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-skip_frame", "nokey",
         "-select_streams", "v:0", "-show_entries", "frame=pts_time",
         "-of", "csv=p=0", "-read_intervals", f"%+{GOP_PROBE_SEC}",
         str(path)],
        capture_output=True, text=True, timeout=60)
    ts = []
    for line in out.stdout.splitlines():
        try:
            ts.append(float(line.strip().rstrip(",")))
        except ValueError:
            continue
    if len(ts) < 2:
        return 0.0
    gaps = sorted(b - a for a, b in zip(ts, ts[1:]) if b > a)
    return gaps[len(gaps) // 2] if gaps else 0.0


_SHOWINFO = re.compile(r"pts_time:\s*([0-9.]+)")


def _ffmpeg_decoders() -> set:
    out = subprocess.run(["ffmpeg", "-hide_banner", "-decoders"],
                         capture_output=True, text=True, timeout=30)
    return {line.split()[1] for line in out.stdout.splitlines()
            if line.startswith(" V") and len(line.split()) > 1}


class FfmpegSource:
    """ffmpeg -> rawvideo pipe. name is 'nvdec', 'ffmpeg', or 'keyframes'.

    Timestamps come from a showinfo filter on stderr rather than being
    computed, so they are exact in every mode (keyframe mode in particular
    yields whatever pts the keyframes actually have)."""

    def __init__(self, path: Path, interval: float, hw: bool,
                 keyframes: bool = False):
        self.info = _ffprobe(path)
        self.fps = self.info["fps"]
        self.video_sec = round(self.info["duration"], 1) or None
        self.interval = interval
        self.t_decode = 0.0
        self.name = "keyframes" if keyframes else ("nvdec" if hw else "ffmpeg")
        # Scale inside ffmpeg when the source is wider than needed. -2
        # keeps the aspect ratio and an even height (required by bgr24
        # conversion of odd-height yuv420p). self.scale maps a box in
        # decoded coordinates back to source coordinates.
        self.src_w, self.src_h = self.info["w"], self.info["h"]
        if DECODE_MAX_W and self.src_w > DECODE_MAX_W:
            self.out_w = DECODE_MAX_W
            self.out_h = int(round(self.src_h * DECODE_MAX_W / self.src_w / 2)) * 2
            self.scale = self.src_w / self.out_w
        else:
            self.out_w, self.out_h = self.src_w, self.src_h
            self.scale = 1.0
        # showinfo logs at info level; "level+" tags every line with its
        # severity so the reader can keep showinfo and real problems and
        # drop the input banner.
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "level+info",
               "-nostdin", "-nostats"]
        if keyframes:
            cmd += ["-skip_frame", "nokey", "-threads", str(DECODE_THREADS)]
            # Keep keyframes at least `interval` apart (minus a little slack
            # so a 1.999 s GOP still passes a 2.0 s interval), passthrough
            # timing so nothing is duplicated to fill a grid.
            vf = (f"select='isnan(prev_selected_t)+gte(t-prev_selected_t,"
                 f"{max(interval - 0.1, 0):.3f})'{self._scale_vf()},showinfo")
            vsync = ["-fps_mode", "passthrough"]
        else:
            if hw:
                dec = _CUVID.get(self.info["codec"])
                if not dec:
                    raise RuntimeError(f"no NVDEC decoder for codec "
                                       f"'{self.info['codec']}'")
                cmd += ["-c:v", dec]
            else:
                cmd += ["-threads", str(DECODE_THREADS)]
            vf = f"fps=1/{interval}{self._scale_vf()},showinfo"
            vsync = []
        cmd += ["-i", str(path), "-an", "-sn", "-dn", "-vf", vf] + vsync + [
                "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)
        # showinfo logs one line per output frame to stderr; read it on a
        # thread so neither pipe can fill and deadlock the other.
        self._pts = []
        self._errs = []
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()

    def _scale_vf(self) -> str:
        return "" if self.scale == 1.0 else f",scale={self.out_w}:{self.out_h}"

    def _drain(self):
        for raw in self.proc.stderr:
            line = raw.decode(errors="replace")
            m = _SHOWINFO.search(line)
            if m and "showinfo" in line:
                self._pts.append(float(m.group(1)))
            elif ("[warning]" in line or "[error]" in line
                  or "[fatal]" in line or "[panic]" in line):
                self._errs.append(re.sub(r"\[(warning|error|fatal|panic)\] ",
                                         "", line.strip()))

    def frames(self):
        w, h = self.out_w, self.out_h
        nbytes = w * h * 3
        n = 0
        while True:
            _d = time.perf_counter()
            buf = self.proc.stdout.read(nbytes)
            self.t_decode += time.perf_counter() - _d
            if len(buf) < nbytes:
                break
            # The showinfo line for frame n is written before frame n's
            # bytes finish crossing the pipe, but the reader thread may not
            # have scheduled yet; wait briefly for it rather than guess.
            for _ in range(200):
                if len(self._pts) > n:
                    break
                time.sleep(0.005)
            ts = self._pts[n] if len(self._pts) > n else n * self.interval
            n += 1
            # frombuffer over a bytes object is read-only; annotate() copies
            # before drawing and every other consumer allocates, so that is
            # fine and saves a memcpy per sampled frame.
            frame = np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            yield ts, frame
        self.proc.stdout.close()
        rc = self.proc.wait()
        self._reader.join(timeout=5)
        err = " | ".join(self._errs)[:300]
        if rc != 0 and n == 0:
            raise RuntimeError(f"{self.name} produced no frames "
                               f"(ffmpeg rc={rc}): {err}")
        if self._errs:
            print(f"WARNING: ffmpeg ({self.name}): {err}", file=sys.stderr)

    def release(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()


class CvSource:
    """The original OpenCV loop: grab() every frame, retrieve() sampled ones."""
    name = "cv2"

    def __init__(self, path: Path, interval: float):
        self.cap = cv2.VideoCapture(str(path))
        if not self.cap.isOpened():
            raise RuntimeError("cv2 cannot open file")
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 25.0
        self.step = max(int(self.fps * interval), 1)
        total = self.cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        self.video_sec = round(total / self.fps, 1) if total else None
        self.t_decode = 0.0
        # OpenCV decodes at source resolution; scaling there would happen
        # after the expensive part, so there is nothing to gain.
        self.scale = 1.0

    def frames(self):
        idx = 0
        while True:
            _d = time.perf_counter()
            ok = self.cap.grab()
            self.t_decode += time.perf_counter() - _d
            if not ok:
                break
            if idx % self.step != 0:
                idx += 1
                continue
            _d = time.perf_counter()
            ok, frame = self.cap.retrieve()
            self.t_decode += time.perf_counter() - _d
            idx += 1
            if ok:
                yield idx / self.fps, frame

    def release(self):
        self.cap.release()


def open_source(path: Path, interval: float):
    """Pick a frame source per DECODE / DECODE_KEYFRAMES. Returns
    (source, effective_interval); raises if the requested decoder cannot
    open the file."""
    have_ffmpeg = shutil.which("ffmpeg") and shutil.which("ffprobe")
    if DECODE == "cv2" or (DECODE == "auto" and not have_ffmpeg):
        return CvSource(path, interval), interval
    if not have_ffmpeg:
        raise RuntimeError(f"DECODE={DECODE} needs ffmpeg and ffprobe on PATH")
    # Keyframe-only decode beats every decoder when the interval allows it.
    if KEYFRAMES in ("auto", "1"):
        gop = _probe_gop(path)
        if KEYFRAMES == "1":
            if gop and interval < gop:
                print(f"NOTE: DECODE_KEYFRAMES=1 raises interval "
                      f"{interval:g}s to the GOP ({gop:.2f}s)",
                      file=sys.stderr)
                interval = gop
            return FfmpegSource(path, interval, hw=False, keyframes=True), interval
        if gop and interval >= gop * KEYFRAME_MIN_RATIO:
            return FfmpegSource(path, interval, hw=False, keyframes=True), interval
    if DECODE == "ffmpeg":
        return FfmpegSource(path, interval, hw=False), interval
    if DECODE == "nvdec":
        hw_ok = bool(os.path.exists("/dev/nvidiactl")) and any(
            d.endswith("_cuvid") for d in _ffmpeg_decoders())
        if not hw_ok:
            raise RuntimeError("DECODE=nvdec but no NVIDIA device or ffmpeg "
                               "lacks *_cuvid decoders (apt install ffmpeg "
                               "libnvcuvid1)")
        return FfmpegSource(path, interval, hw=True), interval
    # auto: NVDEC only on request. Kept as an opt-in because a consumer
    # card's single decoder measured slower than the CPU cores it freed.
    return FfmpegSource(path, interval, hw=False), interval


def iter_frames(src):
    yield from src.frames()


class Detector:
    """Pluggable detector. Default: Ultralytics YOLO (CPU or GPU).

    Replace this class (only this class) to use Coral/TensorRT/remote
    inference. Contract: detect(frame_bgr) -> list of dicts:
        {"label": str, "conf": float, "box": [x1, y1, x2, y2]}
    """

    def __init__(self, model_path: str, classes, conf: float):
        from ultralytics import YOLO
        self.model = YOLO(model_path)
        self.names = self.model.names
        self.classes = set(classes)
        self.conf = conf
        # A class the model was never trained on can never match, and the
        # result is an empty manifest with no error - the worst kind of
        # failure. Say so loudly instead. COCO models know 80 things;
        # 'raccoon' and 'squirrel' are not among them.
        known = set(self.names.values())
        unknown = sorted(self.classes - known)
        if unknown:
            print(f"WARNING: model '{model_path}' has no class(es): "
                  f"{', '.join(unknown)} — these will never match. "
                  f"Run 'make classes' to list what it does know.",
                  file=sys.stderr)
            if not (self.classes & known):
                print("WARNING: none of the configured classes exist in this "
                      "model; every manifest will be empty.", file=sys.stderr)

    def detect(self, frame_bgr):
        results = self.model(frame_bgr, verbose=False, conf=self.conf)
        out = []
        for r in results:
            if r.boxes is None:
                continue
            for b in r.boxes:
                label = self.names[int(b.cls[0])]
                if label not in self.classes:
                    continue
                x1, y1, x2, y2 = (int(v) for v in b.xyxy[0])
                out.append({"label": label,
                            "conf": round(float(b.conf[0]), 3),
                            "box": [x1, y1, x2, y2]})
        return out


def iou(a, b):
    """Intersection-over-union of two [x1,y1,x2,y2] boxes."""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    if inter == 0:
        return 0.0
    area_a = max(0, ax2 - ax1) * max(0, ay2 - ay1)
    area_b = max(0, bx2 - bx1) * max(0, by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union else 0.0


def _centre(box):
    x1, y1, x2, y2 = box
    return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)


def _diag(box):
    x1, y1, x2, y2 = box
    return max(1.0, ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5)


def _predict(tr, t):
    """Extrapolate a track's centre to time t from its last two sightings.

    Constant-velocity guess. With a single sighting there's no velocity yet,
    so it just returns the last known centre.
    """
    cs, ts = tr["centres"], tr["times"]
    if len(cs) < 2:
        return cs[-1]
    dt = ts[-1] - ts[-2]
    if dt <= 0:
        return cs[-1]
    vx = (cs[-1][0] - cs[-2][0]) / dt
    vy = (cs[-1][1] - cs[-2][1]) / dt
    ahead = t - ts[-1]
    return (cs[-1][0] + vx * ahead, cs[-1][1] + vy * ahead)


class Tracker:
    """Groups per-frame detections into tracks.

    Deliberately simple: greedy IoU association against each track's most
    recent box, restricted to matching labels. That is the right complexity
    for SAMPLED frames — a Kalman/ByteTrack motion model assumes a
    continuous stream and mostly mispredicts across a 2-second gap.

    Each track keeps the single highest-confidence frame it has seen, which
    becomes its thumbnail when the file finishes. Only ACTIVE tracks hold a
    frame in memory (usually a handful), so memory stays bounded.
    """

    def __init__(self, iou_thresh=TRACK_IOU, max_gap_obs=TRACK_MAX_GAP_OBS,
                 max_gap_sec=TRACK_MAX_GAP_SEC):
        self.iou_thresh = iou_thresh
        self.max_gap_obs = max_gap_obs
        self.max_gap_sec = max_gap_sec
        self.active = []
        self.closed = []
        self.obs = 0          # inferred frames seen so far
        self._next_id = 1

    def _new_track(self, det, t, frame):
        tr = {"id": self._next_id, "label": det["label"],
              "first_seen": t, "last_seen": t, "frames": 1,
              "conf_max": det["conf"],
              "box_first": det["box"], "box_last": det["box"],
              "centres": [_centre(det["box"])], "diags": [_diag(det["box"])],
              "times": [t], "last_obs": self.obs,
              "_best_conf": det["conf"], "_best_frame": frame,
              "_best_det": det, "_best_t": t}
        self._next_id += 1
        self.active.append(tr)

    def update(self, dets, t, frame):
        """Feed one inferred frame's detections, at video time t (seconds).

        Call this for EVERY inferred frame, including ones with no
        detections, so the observation counter stays in step.
        """
        self.obs += 1
        # Retire tracks not matched for max_gap_obs observations, or beyond
        # the absolute time cap. Gated frames are skipped entirely, so they
        # never count against a track.
        still = []
        for tr in self.active:
            gap_obs = self.obs - tr["last_obs"]
            expired = (gap_obs > self.max_gap_obs
                       or t - tr["last_seen"] > self.max_gap_sec)
            (self.closed if expired else still).append(tr)
        self.active = still

        used = set()
        for det in dets:
            dc = _centre(det["box"])
            dd = _diag(det["box"])
            best, best_score = None, None
            for tr in self.active:
                if tr["id"] in used or tr["label"] != det["label"]:
                    continue
                overlap = iou(tr["box_last"], det["box"])
                # Predict where this track should be now from its recent
                # velocity; a steadily moving object lands near the guess
                # even when its boxes no longer overlap.
                px, py = _predict(tr, t)
                dist = (((dc[0] - px) ** 2 + (dc[1] - py) ** 2) ** 0.5
                        / max(dd, _diag(tr["box_last"])))
                if overlap >= self.iou_thresh or dist <= TRACK_MAX_MOVE:
                    # Prefer higher overlap, then closer to prediction.
                    score = (overlap, -dist)
                    if best_score is None or score > best_score:
                        best, best_score = tr, score
            if best is None:
                self._new_track(det, t, frame)
            else:
                used.add(best["id"])
                best["last_seen"] = t
                best["frames"] += 1
                best["box_last"] = det["box"]
                best["centres"].append(dc)
                best["diags"].append(dd)
                best["times"].append(t)
                best["last_obs"] = self.obs
                best["conf_max"] = max(best["conf_max"], det["conf"])
                if det["conf"] > best["_best_conf"]:
                    best["_best_conf"] = det["conf"]
                    best["_best_frame"] = frame
                    best["_best_det"] = det
                    best["_best_t"] = t

    def finish(self):
        """Close everything; return tracks in time order."""
        self.closed.extend(self.active)
        self.active = []
        return sorted(self.closed, key=lambda tr: (tr["first_seen"], tr["id"]))


def stationary_stats(tr):
    """Numbers behind the stationary decision, kept in the manifest.

    Without these a misclassification is undiagnosable: box_first and
    box_last can be identical while sightings in between wander, and there
    is no way to tell from the output which happened.
    """
    cs = tr["centres"]
    ref = _median(tr["diags"])
    mx = _median([c[0] for c in cs])
    my = _median([c[1] for c in cs])
    d = sorted((((cx - mx) ** 2 + (cy - my) ** 2) ** 0.5) / ref for cx, cy in cs)
    n = len(d)
    pick = lambda p: d[max(0, min(n - 1, int(round(p * (n - 1)))))]
    return {"move_p50": round(pick(0.5), 3),
            "move_p80": round(pick(0.8), 3),
            "move_max": round(d[-1], 3),
            "outliers": sum(1 for x in d if x >= STATIONARY_FRAC),
            "sightings": n}


def is_stationary(tr, frac=STATIONARY_FRAC):
    """True if the box stayed put, judged robustly.

    Measures each sighting's distance from the track's MEDIAN centre, not
    its first (which may itself be a bad frame), and asks whether the 80th
    percentile is small — not the maximum. A detector will occasionally
    return one shifted or resized box for a perfectly stationary object;
    with a max-based test that single frame flips the whole track to
    "moving", which is exactly what happened to parked cars in a garage.

    Distances are normalized by the median box diagonal, so one threshold
    serves both a car filling the frame and a person far down a driveway.
    """
    cs = tr["centres"]
    if len(cs) < 2:
        return False
    ref = _median(tr["diags"])
    mx = _median([c[0] for c in cs])
    my = _median([c[1] for c in cs])
    dists = sorted(((cx - mx) ** 2 + (cy - my) ** 2) ** 0.5 for cx, cy in cs)
    # 80th percentile: tolerate up to a fifth of sightings being outliers.
    idx = max(0, int(round(0.8 * (len(dists) - 1))))
    return (dists[idx] / ref) < frac


def _median(vals):
    s = sorted(vals)
    n = len(s)
    if n == 0:
        return 1.0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def motion_score(prev_gray, gray):
    """Cheap frame-differencing motion gate. Returns changed area in px^2."""
    diff = cv2.absdiff(prev_gray, gray)
    diff = cv2.GaussianBlur(diff, (5, 5), 0)
    _, thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
    return int(cv2.countNonZero(thresh))


def out_paths(input_path: Path, input_root: Path, output_root: Path):
    """Mirror the recording tree under the output root.

    /mnt/recordings/cam03/file.mp4 ->
        /var/detections/cam03/file.mp4.json          (manifest)
        /var/detections/cam03/file.mp4.thumbs/NN.jpg (annotated thumbnails)
    """
    rel = input_path.relative_to(input_root)
    manifest = output_root / rel.parent / (rel.name + ".json")
    thumbs = output_root / rel.parent / (rel.name + ".thumbs")
    return manifest, thumbs


def annotate(frame, dets):
    """Draw boxes on a frame. Detections carry 'box' in source coordinates
    and, when the frame was decoded scaled, 'box_scaled' to draw with."""
    for d in dets:
        x1, y1, x2, y2 = d.get("box_scaled", d["box"])
        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 220, 0), 2)
        cv2.putText(frame, f'{d["label"]} {d["conf"]:.2f}', (x1, max(y1 - 6, 12)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 220, 0), 2)
    h, w = frame.shape[:2]
    if w > THUMB_MAX_W:
        s = THUMB_MAX_W / w
        frame = cv2.resize(frame, (THUMB_MAX_W, int(h * s)))
    return frame


def process(input_path: Path, input_root: Path, output_root: Path,
            save_thumbs: bool) -> int:
    manifest_path, thumbs_dir = out_paths(input_path, input_root, output_root)

    # Resumability: manifest already exists -> this file is done.
    if manifest_path.exists():
        print(f"skip (manifest exists): {input_path}")
        return 0

    # Safety: file may still be written by the recorder.
    age = time.time() - input_path.stat().st_mtime
    if age < MIN_FILE_AGE_SEC:
        print(f"skip (too new, {age:.0f}s): {input_path}")
        return 0

    # Per-camera rules, matched on the filename (camera name lives there).
    cfg = load_rules(RULES_PATH)
    rule = rules_for(input_path.name, cfg)
    classes = rule.get("classes", sorted(CLASSES_OF_INTEREST))
    conf = float(rule.get("conf", CONF_THRESHOLD))
    min_area = int(rule.get("min_area", MOTION_MIN_AREA))
    interval, interval_how = effective_interval(rule, cfg)
    keepalive = float(rule.get("keepalive", KEEPALIVE_SEC))

    # An empty class list means "don't analyze this camera at all". Write a
    # manifest anyway so the file counts as done and isn't retried forever.
    if not classes:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps({
            "source": str(input_path.relative_to(input_root)),
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "skipped": "no classes configured for this camera",
            "worker": {"version": VERSION},
            "rule": rule.get("note", rule.get("pattern", "default")),
            "labels": [], "event_count": 0, "tracks": []}, indent=1))
        print(f"skip (camera excluded by rule): {input_path.name}")
        return 0

    t0 = time.time()
    try:
        src, interval = open_source(input_path, interval)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: cannot open {input_path}: {e}", file=sys.stderr)
        return 1

    detector = Detector(MODEL_PATH, classes, conf)
    tracker = Tracker()
    prev_gray = None
    sampled = 0
    inferred = 0
    # Timing breakdown. Decode vs inference is the number that tells you
    # whether a faster model (or a GPU) would actually help this machine,
    # or whether it is already bottlenecked on pulling frames off disk.
    t_infer = t_gate = t_warmup = 0.0
    last_infer_ts = -1e9   # forces the first sampled frame to be inferred
    keepalives = 0

    for ts_now, frame in iter_frames(src):
        sampled += 1

        _g = time.perf_counter()
        small = cv2.resize(frame,
                           (MOTION_DOWNSCALE_W,
                            int(frame.shape[0] * MOTION_DOWNSCALE_W / frame.shape[1])))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        gated = False
        if prev_gray is not None and motion_score(prev_gray, gray) < min_area:
            gated = True
        prev_gray = gray
        t_gate += time.perf_counter() - _g
        # Even in a dead-still scene, look every `keepalive` seconds so
        # persistent objects (a parked car in a garage) are observed more
        # than once and can therefore be judged stationary.
        if gated and keepalive > 0 and (ts_now - last_infer_ts) >= keepalive:
            gated = False
            keepalives += 1
        if gated:
            continue  # nothing changed and not due a look; skip the model

        _i = time.perf_counter()
        last_infer_ts = ts_now
        dets = detector.detect(frame)
        _took = time.perf_counter() - _i
        # Frames may have been scaled during decode; report boxes in the
        # recording's own coordinates so manifests are comparable across
        # nodes and DECODE_MAX_W settings. Thumbnails keep the scaled
        # frame and are annotated from the scaled boxes, so annotate()
        # runs before this - it does not, so scale a copy for drawing.
        if src.scale != 1.0:
            for d in dets:
                d["box_scaled"] = d["box"]
                d["box"] = [int(round(v * src.scale)) for v in d["box"]]
        inferred += 1
        # The first inference in a process also pays lazy model compilation
        # (OpenVINO in particular can spend seconds there). Counting it in
        # the average makes a file with few inferences look catastrophically
        # slow, so record it separately and average only steady-state calls.
        if inferred == 1:
            t_warmup = _took
        else:
            t_infer += _took
        ts_sec = round(ts_now, 2)
        # Feed every inferred frame, including empty ones, so the tracker's
        # observation counter (which drives track expiry) stays in step.
        # The frame is kept only if it might become a thumbnail; the tracker
        # holds at most one per active track.
        tracker.update(dets, ts_sec, frame if save_thumbs else None)

    src.release()
    t_decode = src.t_decode
    t_thumb = time.perf_counter()

    # Close tracks and turn them into manifest entries. One track = one
    # object seen over time = one event, instead of one event per frame.
    raw = tracker.finish()
    tracks = []
    for tr in raw:
        stationary = is_stationary(tr)
        entry = {
            "id": tr["id"],
            "label": tr["label"],
            "first_seen": tr["first_seen"],
            "last_seen": tr["last_seen"],
            "duration": round(tr["last_seen"] - tr["first_seen"], 2),
            "frames": tr["frames"],
            "conf_max": tr["conf_max"],
            "stationary": stationary,
            "box_first": tr["box_first"],
            "box_last": tr["box_last"],
            "motion": stationary_stats(tr),
        }
        # Thumbnail is the track's highest-confidence sighting, named after
        # the track so image and manifest entry are obviously the same thing.
        if save_thumbs and tr["_best_frame"] is not None:
            thumbs_dir.mkdir(parents=True, exist_ok=True)
            name = (f"track{tr['id']:03d}-{tr['label']}"
                    f"-t{tr['_best_t']:.0f}s.jpg")
            cv2.imwrite(str(thumbs_dir / name),
                        annotate(tr["_best_frame"].copy(), [tr["_best_det"]]),
                        [cv2.IMWRITE_JPEG_QUALITY, 80])
            entry["thumb"] = name
        tracks.append(entry)

    labels = sorted({tr["label"] for tr in tracks})
    moving = [tr for tr in tracks if not tr["stationary"]]
    t_thumb = time.perf_counter() - t_thumb
    wall = time.time() - t0
    video_sec = src.video_sec
    timing = {
        "wall_sec": round(wall, 2),
        "video_sec": video_sec,
        # Which decoder produced the frames: keyframes, ffmpeg, nvdec or
        # cv2 - and at what width, since frames may be scaled during
        # decode. Boxes in this manifest are always source coordinates.
        "decoder": src.name,
        "decode_width": getattr(src, "out_w", None),
        # >1 means faster than realtime: 6.7 == one core keeps up with ~6
        # cameras of continuous recording. The headline portability number.
        "realtime_factor": (round(video_sec / wall, 1)
                            if video_sec and wall > 0 else None),
        "decode_sec": round(t_decode, 2),
        "motion_gate_sec": round(t_gate, 2),
        "inference_sec": round(t_infer, 2),
        # First call only: model load/compile, paid once per file because
        # each file is its own process. Excluded from ms_per_inference.
        "warmup_sec": round(t_warmup, 2),
        "thumbs_sec": round(t_thumb, 2),
        "frames_sampled": sampled,
        "frames_inferred": inferred,
        "keepalive_inferences": keepalives,
        "ms_per_inference": (round(t_infer * 1000 / (inferred - 1), 1)
                             if inferred > 1 else None),
    }
    manifest = {
        "source": str(input_path.relative_to(input_root)),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "labels": labels,
        "event_count": len(tracks),
        "moving_count": len(moving),
        "stationary_count": len(tracks) - len(moving),
        "tracks": tracks,
        "timing": timing,
        # Which machine produced this, so timings from a mixed fleet can be
        # told apart after the fact.
        "host": {"name": socket.gethostname(), "model": MODEL_PATH},
        "worker": {"version": VERSION,
                   "model": MODEL_PATH,
                   "rule": rule.get("note", rule.get("pattern", "default")),
                   "classes": sorted(classes),
                   "frame_interval_sec": round(interval, 3),
                   # 'rule', 'gpu_interval', or 'scale=N': why this
                   # interval, so a dense GPU sample and a sparse CPU
                   # sample of the same camera are distinguishable.
                   "interval_from": interval_how,
                   "conf_threshold": conf,
                   "min_area": min_area,
                   "track_iou": TRACK_IOU,
                   "stationary_frac": STATIONARY_FRAC},
    }

    # Write atomically so the viewer never reads a half-written manifest.
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=1))
    tmp.rename(manifest_path)
    stat_n = len(tracks) - len(moving)
    rt = timing["realtime_factor"]
    print(f"done: {input_path.name} tracks={len(tracks)} "
          f"(moving={len(moving)}, stationary={stat_n}) labels={labels} "
          f"| {timing['wall_sec']}s"
          + (f" for {timing['video_sec']}s video = {rt}x realtime" if rt else "")
          + f" | decode {timing['decode_sec']}s ({src.name}), gate {timing['motion_gate_sec']}s, "
          f"infer {timing['inference_sec']}s"
          + (f", warmup {timing['warmup_sec']}s" if t_warmup else "")
          + (f" ({inferred} @ {timing['ms_per_inference']}ms)"
             if timing['ms_per_inference'] else f" ({inferred} inference)"))
    return 0


MAX_ATTEMPTS = 3   # failures tolerated before a file is written off


def record_failure(input_path, input_root, output_root, err: str) -> int:
    """Bounded retry for files that fail.

    Without this a corrupt recording, or a detector crash, leaves no manifest
    and is retried by every sweep forever - a mid-file crash costs a full
    model load each time. Track attempts in a sidecar; after MAX_ATTEMPTS
    write an error manifest so the file counts as done. --force clears it if
    you want to retry later (e.g. after fixing the file or the model).
    """
    manifest_path, _ = out_paths(input_path, input_root, output_root)
    marker = manifest_path.with_suffix(manifest_path.suffix + ".failed")
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        state = json.loads(marker.read_text()) if marker.exists() else {}
    except (json.JSONDecodeError, OSError):
        state = {}
    attempts = int(state.get("attempts", 0)) + 1
    state.update({"attempts": attempts, "last_error": err[-500:],
                  "last_attempt": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
    if attempts < MAX_ATTEMPTS:
        marker.write_text(json.dumps(state, indent=1))
        print(f"FAILED ({attempts}/{MAX_ATTEMPTS}, will retry): "
              f"{input_path.name}: {err.splitlines()[-1] if err else '?'}",
              file=sys.stderr)
        return 1
    # Give up: an error manifest makes the sweep stop retrying.
    manifest = {
        "source": str(input_path.relative_to(input_root)),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "error": err.splitlines()[-1] if err else "unknown",
        "attempts": attempts,
        "labels": [], "event_count": 0, "moving_count": 0,
        "stationary_count": 0, "tracks": [],
        "host": {"name": socket.gethostname(), "model": MODEL_PATH},
        "worker": {"version": VERSION, "model": MODEL_PATH},
    }
    tmp = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=1))
    tmp.rename(manifest_path)
    marker.unlink(missing_ok=True)
    print(f"FAILED ({attempts}/{MAX_ATTEMPTS}, giving up, error manifest "
          f"written): {input_path.name}: {manifest['error']}", file=sys.stderr)
    return 1


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--input", required=True, type=Path)
    ap.add_argument("--input-root", required=True, type=Path)
    ap.add_argument("--output-root", required=True, type=Path)
    ap.add_argument("--no-thumbs", action="store_true")
    a = ap.parse_args()
    inp, root, out = (a.input.resolve(), a.input_root.resolve(),
                      a.output_root.resolve())
    try:
        rc = process(inp, root, out, save_thumbs=not a.no_thumbs)
    except Exception:  # noqa: BLE001 - anything at all must not loop forever
        import traceback
        rc = record_failure(inp, root, out, traceback.format_exc())
    else:
        if rc != 0:
            rc = record_failure(inp, root, out, "worker returned non-zero "
                                "(unreadable or unsupported file)")
        else:
            # A success clears any earlier failure marker.
            mp, _ = out_paths(inp, root, out)
            mp.with_suffix(mp.suffix + ".failed").unlink(missing_ok=True)
    sys.exit(rc)


if __name__ == "__main__":
    main()
