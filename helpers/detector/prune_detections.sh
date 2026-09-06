#!/usr/bin/env bash
# prune_detections.sh - retention policy for the /var/detections tree.
#
# Two-tier policy:
#   * Thumbnails are the bulky part -> keep THUMB_DAYS (default: match your
#     recording retention, 30 days, since a thumbnail without its video is
#     of limited use).
#   * Manifests are tiny JSON -> keep MANIFEST_DAYS longer (default 90) so
#     you retain a searchable index of "a person was on cam03 at 14:05"
#     even after the video itself has been recycled.
#   * Also drop manifests/thumbs whose SOURCE video no longer exists, once
#     the manifest itself passes THUMB_DAYS (prevents orphans if you shorten
#     recording retention).
#
# Cron (nightly, after the recording cleanup on the viewer box):
#   30 2 * * *  detector  /opt/detection/prune_detections.sh
set -euo pipefail

DETECTIONS=${DETECTIONS:-/var/detections}
RECORDINGS=${RECORDINGS:-/mnt/recordings}
THUMB_DAYS=${THUMB_DAYS:-30}
MANIFEST_DAYS=${MANIFEST_DAYS:-90}

# 1. Old thumbnails
find "$DETECTIONS" -type d -name '*.thumbs' -mtime +"$THUMB_DAYS" \
  -exec rm -rf {} + 2>/dev/null || true

# 2. Old manifests
find "$DETECTIONS" -type f -name '*.json' -mtime +"$MANIFEST_DAYS" -delete

# 3. Orphans: manifest older than THUMB_DAYS whose source video is gone
find "$DETECTIONS" -type f -name '*.json' -mtime +"$THUMB_DAYS" -print0 |
while IFS= read -r -d '' m; do
  rel=${m#"$DETECTIONS"/}; rel=${rel%.json}
  [[ -e "$RECORDINGS/$rel" ]] || rm -f "$m"
done

# 4. Empty directories left behind
find "$DETECTIONS" -mindepth 1 -type d -empty -delete
echo "prune complete: $(du -sh "$DETECTIONS" | cut -f1) in $DETECTIONS"
