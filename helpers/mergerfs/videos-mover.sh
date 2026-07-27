#!/bin/bash
# videos-mover.sh - tiered storage mover for rtmp-nginx-viewer
#
# Moves finished recordings from the fast (SSD) branch of a mergerfs pool
# to the slow (HDD) branch, and optionally on to an NFS archive branch.
# Run from cron as root. See helpers/mergerfs/README.md for full setup.

### --- Configuration -------------------------------------------------------

# Branch paths (must match your mergerfs fstab entry)
SSD=/mnt/cache/videos
HDD=/mnt/hdd/videos

# Optional third tier. Leave empty to disable archiving.
NFS=

# Hours of footage to keep on the SSD tier before moving to HDD
KEEP_HOURS=48

# SSD filesystem usage percent that triggers extra oldest-first eviction
FILL_LIMIT=75

# Days of footage to keep on the HDD tier before archiving to NFS
# (only used when NFS is set)
ARCHIVE_DAYS=30

# Days of footage to keep on the final tier before deletion.
# Applies to NFS when set, otherwise to the HDD. Empty disables purging.
RETENTION_DAYS=

### --- End configuration ---------------------------------------------------

set -u

SRC="$SSD/recordings"
DST="$HDD/recordings"

log() { echo "$(date '+%F %T') $*"; }

move_file() {
    # $1 = relative path, $2 = source root, $3 = dest root
    mkdir -p "$3/$(dirname "$1")"
    rsync -a --remove-source-files "$2/$1" "$3/$1"
}

# --- Stage 1: age-based move, SSD -> HDD -----------------------------------
# Never touch files modified in the last 10 minutes; nginx-rtmp may still
# be writing them.
find "$SRC" -type f -mmin +$((KEEP_HOURS * 60)) -printf '%P\n' | while read -r f; do
    log "age-move: $f"
    move_file "$f" "$SRC" "$DST"
done

# --- Stage 2: pressure eviction, oldest first ------------------------------
while [ "$(df --output=pcent "$SSD" | tail -1 | tr -dc '0-9')" -gt "$FILL_LIMIT" ]; do
    f=$(find "$SRC" -type f -mmin +10 -printf '%T@ %P\n' | sort -n | head -1 | cut -d' ' -f2-)
    [ -z "$f" ] && break
    log "pressure-move: $f"
    move_file "$f" "$SRC" "$DST"
done

# --- Stage 3 (optional): archive, HDD -> NFS -------------------------------
if [ -n "$NFS" ]; then
    if mountpoint -q "$(dirname "$NFS")" || mountpoint -q "$NFS"; then
        find "$DST" -type f -mtime +"$ARCHIVE_DAYS" -printf '%P\n' | while read -r f; do
            log "archive: $f"
            move_file "$f" "$DST" "$NFS/recordings"
        done
        find "$DST" -mindepth 1 -type d -empty -delete
    else
        log "WARNING: NFS archive not mounted, skipping archive stage"
    fi
fi

# --- Stage 4 (optional): retention purge on the final tier -----------------
if [ -n "$RETENTION_DAYS" ]; then
    if [ -n "$NFS" ]; then
        FINAL="$NFS/recordings"
    else
        FINAL="$DST"
    fi
    find "$FINAL" -type f -mtime +"$RETENTION_DAYS" -print -delete | while read -r f; do
        log "purge: $f"
    done
    find "$FINAL" -mindepth 1 -type d -empty -delete
fi

# --- Cleanup: remove empty dirs left behind on the SSD tier ----------------
find "$SRC" -mindepth 1 -type d -empty -delete

exit 0
