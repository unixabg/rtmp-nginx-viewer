#!/usr/bin/env bash
set -euo pipefail

SRC_PATH="${1:-}"
BASENAME="${2:-}"
APP="${3:-cam}"
STREAM="${4:-unknown}"

# Write what we are passed out to examine
echo "$SRC_PATH $BASENAME $APP $STREAM" > /tmp/$STREAM.values

THUMB_DIR="/videos/thumbnails"
mkdir -p "$THUMB_DIR"

# lock per stream to avoid races if multiple segments finish together
LOCK="/var/lock/thumb-${APP}-${STREAM}.lock"
exec 9>"$LOCK" || true
flock -x 9 || true

# skip if source missing/empty
[[ -s "$SRC_PATH" ]] || exit 0

# pick a sane seek (min(clip/2, 10s); fallback 1s)
dur="$(ffprobe -v error -select_streams v:0 -show_entries format=duration \
      -of default=nw=1:nk=1 "$SRC_PATH" 2>/dev/null || echo 0)"
seek="$(awk -v d="${dur:-0}" 'BEGIN{ if(d<=0){print 1} else {m=d/2; if(m>10){m=10} print m } }')"

tmp="$(mktemp --suffix=.jpg)"
latest="$THUMB_DIR/${STREAM}.jpg"

# try to grab one frame and scale reasonably
if ffmpeg -y -ss "$seek" -i "$SRC_PATH" -frames:v 1 -q:v 2 -vf "scale='min(1280,iw)':-2" "$tmp" >/dev/null 2>&1; then
  [[ -s "$tmp" ]] || { rm -f "$tmp"; exit 0; }
  # atomically publish the "one-per-cam" thumbnail
  mv -f "$tmp" "$latest"
else
  rm -f "$tmp" 2>/dev/null || true
fi

