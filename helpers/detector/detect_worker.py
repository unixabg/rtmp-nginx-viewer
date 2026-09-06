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
            "labels": [], "event_count": 0, "events": []}, indent=1))
        print(f"skip (camera excluded by rule): {input_path.name}")
        return 0

    cap = cv2.VideoCapture(str(input_path))
    if not cap.isOpened():
        print(f"ERROR: cannot open {input_path}", file=sys.stderr)
        return 1

    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    step = max(int(fps * interval), 1)

    detector = Detector(MODEL_PATH, classes, conf)
    events = []
    prev_gray = None
    frame_idx = 0
    sampled = 0
    inferred = 0
    thumb_idx = 0
    t0 = time.time()

    while True:
        ok = cap.grab()
        if not ok:
            break
        if frame_idx % step != 0:
            frame_idx += 1
            continue
        ok, frame = cap.retrieve()
        frame_idx += 1
        if not ok:
            continue
        sampled += 1

        small = cv2.resize(frame,
                           (MOTION_DOWNSCALE_W,
                            int(frame.shape[0] * MOTION_DOWNSCALE_W / frame.shape[1])))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        if prev_gray is not None:
            if motion_score(prev_gray, gray) < min_area:
                prev_gray = gray
                continue  # nothing changed; don't wake the model
        prev_gray = gray

        dets = detector.detect(frame)
        inferred += 1
        if not dets:
            continue

        ts_sec = round(frame_idx / fps, 2)
        ev = {"t": ts_sec, "detections": dets}
        if save_thumbs:
            thumbs_dir.mkdir(parents=True, exist_ok=True)
            tpath = thumbs_dir / f"{thumb_idx:04d}.jpg"
            cv2.imwrite(str(tpath), annotate(frame.copy(), dets),
                        [cv2.IMWRITE_JPEG_QUALITY, 80])
            ev["thumb"] = tpath.name
            thumb_idx += 1
        events.append(ev)

    cap.release()

    labels = sorted({d["label"] for e in events for d in e["detections"]})
    manifest = {
        "source": str(input_path.relative_to(input_root)),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "duration_sampled_frames": sampled,
        "frames_inferred": inferred,
        "wall_time_sec": round(time.time() - t0, 1),
        "labels": labels,
        "event_count": len(events),
        "events": events,
        "worker": {"model": MODEL_PATH,
                   "rule": rule.get("note", rule.get("pattern", "default")),
                   "classes": sorted(classes),
                   "frame_interval_sec": interval,
                   "conf_threshold": conf,
                   "min_area": min_area},
    }

    # Write atomically so the viewer never reads a half-written manifest.
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=1))
    tmp.rename(manifest_path)
    print(f"done: {input_path.name} events={len(events)} labels={labels} "
          f"({manifest['wall_time_sec']}s)")
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
