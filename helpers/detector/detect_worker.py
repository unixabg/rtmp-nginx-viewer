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
import socket
import sys
import time
from pathlib import Path

import cv2  # opencv-python-headless

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

# ------------------------------------------------------------- tracking
# Per-frame detections are grouped into TRACKS so one object seen across
# many sampled frames is one event, not dozens. Association is IoU-based
# against the previous sighting: same label + overlapping box = same object.
TRACK_IOU = 0.3          # min overlap to call it the same object
TRACK_MAX_GAP_SEC = 6.0  # close a track after this long unseen (occlusion)
# With sampled frames, a fast object can move further than its own width
# between samples, so boxes don't overlap and IoU alone fragments the track.
# Fall back to a centre-distance gate, measured against the position
# PREDICTED from the track's recent velocity, in units of box diagonal.
TRACK_MAX_MOVE = 1.2
# "Stationary" = the box centre never moved more than this fraction of the
# box's own size over the track's life. Parked cars, furniture, etc.
STATIONARY_FRAC = 0.25

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

    def __init__(self, iou_thresh=TRACK_IOU, max_gap=TRACK_MAX_GAP_SEC):
        self.iou_thresh = iou_thresh
        self.max_gap = max_gap
        self.active = []
        self.closed = []
        self._next_id = 1

    def _new_track(self, det, t, frame):
        tr = {"id": self._next_id, "label": det["label"],
              "first_seen": t, "last_seen": t, "frames": 1,
              "conf_max": det["conf"],
              "box_first": det["box"], "box_last": det["box"],
              "centres": [_centre(det["box"])], "diags": [_diag(det["box"])],
              "times": [t],
              "_best_conf": det["conf"], "_best_frame": frame,
              "_best_det": det, "_best_t": t}
        self._next_id += 1
        self.active.append(tr)

    def update(self, dets, t, frame):
        """Feed one sampled frame's detections, at video time t (seconds)."""
        # Retire tracks unseen for longer than the gap tolerance.
        still = []
        for tr in self.active:
            (still if t - tr["last_seen"] <= self.max_gap
             else self.closed).append(tr)
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


def is_stationary(tr, frac=STATIONARY_FRAC):
    """True if the box centre never wandered far relative to its own size.

    Normalizing by box size is what lets one threshold serve both a car
    filling the frame and a person far down a driveway.
    """
    cs = tr["centres"]
    if len(cs) < 2:
        return False
    ref = sum(tr["diags"]) / len(tr["diags"])
    x0, y0 = cs[0]
    worst = max(((cx - x0) ** 2 + (cy - y0) ** 2) ** 0.5 for cx, cy in cs)
    return (worst / ref) < frac


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
    for d in dets:
        x1, y1, x2, y2 = d["box"]
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
    interval = float(rule.get("interval", FRAME_INTERVAL_SEC))

    # An empty class list means "don't analyze this camera at all". Write a
    # manifest anyway so the file counts as done and isn't retried forever.
    if not classes:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps({
            "source": str(input_path.relative_to(input_root)),
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "skipped": "no classes configured for this camera",
            "rule": rule.get("note", rule.get("pattern", "default")),
            "labels": [], "event_count": 0, "tracks": []}, indent=1))
        print(f"skip (camera excluded by rule): {input_path.name}")
        return 0

    cap = cv2.VideoCapture(str(input_path))
    if not cap.isOpened():
        print(f"ERROR: cannot open {input_path}", file=sys.stderr)
        return 1

    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    step = max(int(fps * interval), 1)

    detector = Detector(MODEL_PATH, classes, conf)
    tracker = Tracker()
    prev_gray = None
    frame_idx = 0
    sampled = 0
    inferred = 0
    t0 = time.time()
    # Timing breakdown. Decode vs inference is the number that tells you
    # whether a faster model (or a GPU) would actually help this machine,
    # or whether it is already bottlenecked on pulling frames off disk.
    t_decode = t_infer = t_gate = 0.0
    frame_total = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0

    while True:
        _d = time.perf_counter()
        ok = cap.grab()
        t_decode += time.perf_counter() - _d
        if not ok:
            break
        if frame_idx % step != 0:
            frame_idx += 1
            continue
        _d = time.perf_counter()
        ok, frame = cap.retrieve()
        t_decode += time.perf_counter() - _d
        frame_idx += 1
        if not ok:
            continue
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
        if gated:
            continue  # nothing changed; don't wake the model

        _i = time.perf_counter()
        dets = detector.detect(frame)
        t_infer += time.perf_counter() - _i
        inferred += 1
        if not dets:
            continue

        ts_sec = round(frame_idx / fps, 2)
        # Keep the frame only if we might need it as a thumbnail; the tracker
        # holds at most one frame per active track.
        tracker.update(dets, ts_sec, frame if save_thumbs else None)

    cap.release()
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
    video_sec = round(frame_total / fps, 1) if frame_total and fps else None
    timing = {
        "wall_sec": round(wall, 2),
        "video_sec": video_sec,
        # >1 means faster than realtime: 6.7 == one core keeps up with ~6
        # cameras of continuous recording. The headline portability number.
        "realtime_factor": (round(video_sec / wall, 1)
                            if video_sec and wall > 0 else None),
        "decode_sec": round(t_decode, 2),
        "motion_gate_sec": round(t_gate, 2),
        "inference_sec": round(t_infer, 2),
        "thumbs_sec": round(t_thumb, 2),
        "frames_sampled": sampled,
        "frames_inferred": inferred,
        "ms_per_inference": (round(t_infer * 1000 / inferred, 1)
                             if inferred else None),
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
        "worker": {"model": MODEL_PATH,
                   "rule": rule.get("note", rule.get("pattern", "default")),
                   "classes": sorted(classes),
                   "frame_interval_sec": interval,
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
          + f" | decode {timing['decode_sec']}s, gate {timing['motion_gate_sec']}s, "
          f"infer {timing['inference_sec']}s"
          + (f" ({inferred} @ {timing['ms_per_inference']}ms)" if inferred else ""))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--input", required=True, type=Path)
    ap.add_argument("--input-root", required=True, type=Path)
    ap.add_argument("--output-root", required=True, type=Path)
    ap.add_argument("--no-thumbs", action="store_true")
    a = ap.parse_args()
    sys.exit(process(a.input.resolve(), a.input_root.resolve(),
                     a.output_root.resolve(), save_thumbs=not a.no_thumbs))


if __name__ == "__main__":
    main()
