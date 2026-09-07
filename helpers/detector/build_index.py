#!/usr/bin/env python3
"""
build_index.py - turn a tree of detection manifests into small JSON files the
browse page can load.

A browser can't list a directory, so the page needs an index. Rather than
adding a server-side API to the recorder box, this writes static JSON that
nginx serves like any other file - the web layer stays entirely passive.

Output, under <detections>/index/:
    days.json          [{"day": "2026-09-06", "tracks": 412, "cameras": [...]}]
    2026-09-06.json    every track seen that day, flattened for display

Sharding by day matters at scale: 10,000 recordings is roughly 20,000 tracks,
which is a multi-megabyte file if written as one blob. Per-day files keep each
page load small and let the UI fetch only what's being looked at.

Incremental by default: a day is rebuilt only if one of its manifests is newer
than the shard. Use --all to force a full rebuild (after changing this script,
or the manifest format).

Usage:
    build_index.py --detections /videos/detections
    build_index.py --detections /videos/detections --all
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

# Camera name and timestamp both live in the recording's filename:
#   Camera32-1788648910-20260905-175510.mp4
# Take the LAST two dash-separated fields as date and time - an epoch field
# earlier in the name would otherwise match first.
STAMP = re.compile(r"^(?P<camera>.+?)-.*?(?P<date>\d{8})-(?P<time>\d{6})$")


def parse_name(stem: str):
    """-> (camera, 'YYYY-MM-DD', 'HH:MM:SS') or None if unparseable."""
    m = STAMP.match(stem)
    if not m:
        return None
    d, t = m.group("date"), m.group("time")
    return (m.group("camera"),
            f"{d[:4]}-{d[4:6]}-{d[6:]}",
            f"{t[:2]}:{t[2:4]}:{t[4:]}")


def clock_plus(hhmmss: str, seconds: float) -> str:
    """Wall-clock time of an offset into a recording."""
    h, m, s = (int(x) for x in hhmmss.split(":"))
    total = h * 3600 + m * 60 + s + int(seconds)
    total %= 86400
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def main():
    ap = argparse.ArgumentParser(description="build the browse index")
    ap.add_argument("--detections", required=True, type=Path)
    ap.add_argument("--all", action="store_true",
                    help="rebuild every day, not just changed ones")
    a = ap.parse_args()

    root: Path = a.detections.resolve()
    if not root.is_dir():
        print(f"no such directory: {root}", file=sys.stderr)
        return 1
    index_dir = root / "index"
    index_dir.mkdir(exist_ok=True)

    # Group manifests by day, tracking the newest mtime per day so we can
    # skip days that haven't changed.
    by_day = defaultdict(list)
    newest = defaultdict(float)
    unparsed = 0
    for man in root.rglob("*.json"):
        if index_dir in man.parents:
            continue
        parsed = parse_name(man.name.replace(".mp4.json", "").replace(".json", ""))
        if not parsed:
            unparsed += 1
            continue
        camera, day, clock = parsed
        by_day[day].append((man, camera, clock))
        newest[day] = max(newest[day], man.stat().st_mtime)

    days_out = []
    rebuilt = 0
    for day in sorted(by_day, reverse=True):
        shard = index_dir / f"{day}.json"
        stale = a.all or not shard.exists() or \
            shard.stat().st_mtime < newest[day]

        if not stale:
            try:
                prev = json.loads(shard.read_text())
                days_out.append({"day": day,
                                 "tracks": len(prev.get("tracks", [])),
                                 "cameras": prev.get("cameras", [])})
                continue
            except (json.JSONDecodeError, OSError):
                stale = True  # unreadable shard: rebuild it

        rows = []
        cameras = set()
        for man, camera, clock in sorted(by_day[day], key=lambda x: x[2]):
            try:
                data = json.loads(man.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            cameras.add(camera)
            src = data.get("source", "")
            rel = str(man.relative_to(root))
            thumbs_rel = rel[:-len(".json")] + ".thumbs"
            for tr in data.get("tracks", []):
                rows.append({
                    "camera": camera,
                    "day": day,
                    "clock": clock_plus(clock, tr.get("first_seen", 0)),
                    "label": tr.get("label", "?"),
                    "conf": tr.get("conf_max"),
                    "dur": tr.get("duration", 0),
                    "still": bool(tr.get("stationary")),
                    "t": tr.get("first_seen", 0),
                    "src": src,
                    "thumb": (f"{thumbs_rel}/{tr['thumb']}"
                              if tr.get("thumb") else None),
                })

        rows.sort(key=lambda r: r["clock"])
        shard.write_text(json.dumps(
            {"day": day, "cameras": sorted(cameras), "tracks": rows},
            separators=(",", ":")))
        days_out.append({"day": day, "tracks": len(rows),
                         "cameras": sorted(cameras)})
        rebuilt += 1

    (index_dir / "days.json").write_text(json.dumps(days_out,
                                                    separators=(",", ":")))
    total = sum(d["tracks"] for d in days_out)
    print(f"index: {len(days_out)} day(s), {total} track(s), "
          f"{rebuilt} shard(s) rebuilt"
          + (f", {unparsed} manifest(s) with unparseable names" if unparsed else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
