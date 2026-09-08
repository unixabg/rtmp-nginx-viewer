#!/usr/bin/env bash
# run_detection.sh - fan detect_worker.py out over recordings with GNU Parallel.
#
# Local:    ./run_detection.sh
# Cluster:  ./run_detection.sh --nodes /etc/detection/nodes.txt
#           (nodes.txt is a GNU Parallel --sshloginfile, e.g. "4/gpu-box" per
#            line = 4 jobs on host gpu-box. Every node must have the SAME
#            /mnt/recordings mount and this repo at the SAME path.)
#
# TARGETED RUNS - ask for one camera and/or a date-time range instead of
# everything. Selection is on the timestamp IN THE FILENAME (authoritative;
# mtime drifts if files are ever copied), format ...-YYYYMMDD-HHMMSS.mp4:
#
#   ./run_detection.sh --camera Camera32
#   ./run_detection.sh --camera Camera32 --from 2026-09-05 --to 2026-09-06
#   ./run_detection.sh --from '2026-09-05 17:00' --to '2026-09-05 18:30'
#   ./run_detection.sh --camera 'Camera3?' --from 2026-09-01 --dry-run
#
#   --camera  glob matched against the filename's leading name field; a bare
#             name like Camera32 is auto-anchored to "Camera32-*"
#   --from    inclusive lower bound; --to is EXCLUSIVE upper bound
#             accepts YYYY-MM-DD or 'YYYY-MM-DD HH:MM[:SS]'
#   --force   reprocess files that already have manifests (deletes them
#             first). Use after changing a camera's rules in cameras.json.
#   --limit N process at most N files this run. Files are ordered NEWEST
#             FIRST, so fresh recordings are always taken before backlog:
#             a scheduled sweep stays bounded and can never be monopolised
#             by history, while spare capacity still chips away at it.
#             Size it above your arrival rate (cameras x files-per-run) or
#             the backlog will never shrink.
#   --dry-run list what would be processed and exit
#
# Resumable by construction:
#   * worker skips files whose manifest already exists
#   * worker skips files newer than MIN_FILE_AGE_SEC (recorder may hold them)
#   * a killed run is simply re-run; done files are skipped by the worker
#
# Run from cron on the detection box, e.g. every 10 minutes:
#   */10 * * * *  detector  flock -n /run/lock/detect.lock \
#       /opt/detection/run_detection.sh >> /var/log/detection/run.log 2>&1
set -euo pipefail

RECORDINGS=${RECORDINGS:-/mnt/recordings}     # read-only NFS mount
DETECTIONS=${DETECTIONS:-/var/detections}     # writable output (NOT on the ro mount)
JOBS=${JOBS:--1}                              # -1 = one job per core minus one
MIN_AGE_MIN=${MIN_AGE_MIN:-2}                 # mirror of worker's MIN_FILE_AGE_SEC
JOBLOG=${JOBLOG:-/var/detections/.joblog}
WORKER="$(cd "$(dirname "$0")" && pwd)/detect_worker.py"
# Prefer the venv that the README installs alongside this script; PYBIN can
# override, and we fall back to system python3. Because the venv lives at the
# same path on every node, this also picks the right interpreter over ssh —
# no 'activate' needed anywhere (cron included).
DEFAULT_VENV_PY="$(dirname "$WORKER")/venv/bin/python3"
PYBIN=${PYBIN:-$([[ -x "$DEFAULT_VENV_PY" ]] && echo "$DEFAULT_VENV_PY" || echo python3)}
SSHLOGINFILE=""
CAMERA=""       # glob against the filename's name field
FROM_TS=""      # YYYYMMDDHHMMSS, inclusive
TO_TS=""        # YYYYMMDDHHMMSS, exclusive
FORCE=0
DRYRUN=0
LIMIT=0        # 0 = no cap

# Normalize 'YYYY-MM-DD[ HH:MM[:SS]]' -> YYYYMMDDHHMMSS so timestamps compare
# as plain integers. A bare date becomes midnight, which is why --to is
# exclusive: '--from 2026-09-05 --to 2026-09-06' is exactly one full day.
norm_ts() {
  local s="${1//[-:\/]/}"; s="${s// /}"; s="${s//T/}"
  if [[ ! $s =~ ^[0-9]+$ ]]; then
    echo "bad timestamp: $1 (use YYYY-MM-DD or 'YYYY-MM-DD HH:MM')" >&2
    exit 2
  fi
  case ${#s} in
    8)  echo "${s}000000" ;;   # YYYYMMDD
    12) echo "${s}00" ;;       # YYYYMMDDHHMM
    14) echo "$s" ;;           # YYYYMMDDHHMMSS
    *)  echo "bad timestamp: $1 (use YYYY-MM-DD or 'YYYY-MM-DD HH:MM')" >&2
        exit 2 ;;
  esac
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --nodes)   SSHLOGINFILE="$2"; shift 2 ;;
    --camera)  CAMERA="$2"; shift 2 ;;
    --from)    FROM_TS=$(norm_ts "$2"); shift 2 ;;
    --to)      TO_TS=$(norm_ts "$2"); shift 2 ;;
    --force)   FORCE=1; shift ;;
    --limit)   LIMIT="$2"; shift 2 ;;
    --dry-run) DRYRUN=1; shift ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# A bare camera name is anchored so --camera Camera3 doesn't also match
# Camera30..39; pass an explicit glob (e.g. 'Camera3?') if you want a range.
[[ -n "$CAMERA" && "$CAMERA" != *[\*\?\[]* ]] && CAMERA="${CAMERA}-*"

mkdir -p "$DETECTIONS"

# Eligible = closed mp4s old enough to be safely readable, newest first so the
# most recent footage gets detections soonest. The manifest-exists check lives
# in the worker, so files already done cost only a fork.
NAME_GLOB='*.mp4'
[[ -n "$CAMERA" ]] && NAME_GLOB="${CAMERA%.mp4}*.mp4"

mapfile -t CANDIDATES < <(
  find "$RECORDINGS" -type f -name "$NAME_GLOB" -mmin +"$MIN_AGE_MIN" -print
)

# Time-range filter on the filename's trailing -YYYYMMDD-HHMMSS. Files whose
# names don't carry a parseable timestamp are kept when no range was asked
# for, and skipped when one was (we can't place them in time).
KEYED=()
UNDATED=0
for f in "${CANDIDATES[@]}"; do
  # Take the LAST two dash-separated fields (date, time) rather than a
  # leftmost regex match - names like Camera32-1788648910-20260905-175510
  # contain an epoch field whose digits would otherwise match first.
  base=${f##*/}; stem=${base%.*}
  hhmmss=${stem##*-}; rest=${stem%-*}; yyyymmdd=${rest##*-}
  if [[ $yyyymmdd =~ ^[0-9]{8}$ && $hhmmss =~ ^[0-9]{6}$ ]]; then
    ts="${yyyymmdd}${hhmmss}"
  else
    # No timestamp in the name: keep it only when no range was asked for,
    # and sort it last (key 0) since we can't place it in time.
    UNDATED=$((UNDATED + 1))
    [[ -n "$FROM_TS" || -n "$TO_TS" ]] && continue
    KEYED+=("00000000000000 $f"); continue
  fi
  [[ -n "$FROM_TS" && "$ts" < "$FROM_TS" ]] && continue
  [[ -n "$TO_TS"   && ! "$ts" < "$TO_TS"  ]] && continue
  KEYED+=("$ts $f")
done

# Newest first, by the timestamp IN THE NAME rather than mtime: mtime is the
# time the bytes last changed, which diverges the moment a file is copied,
# restored, or touched. The name is what the recorder meant.
mapfile -t FILES < <(printf '%s\n' "${KEYED[@]}" | sort -rn | cut -d' ' -f2-)

sel="all cameras"; [[ -n "$CAMERA" ]] && sel="camera glob '$CAMERA'"
rng="all time"
[[ -n "$FROM_TS" || -n "$TO_TS" ]] && rng="${FROM_TS:-beginning} .. ${TO_TS:-now}"
echo "selection: $sel, $rng"
[[ $UNDATED -gt 0 ]] && echo "note: $UNDATED file(s) skipped - no timestamp in name"

# --force: drop existing manifests for the selection so the worker redoes
# them (it skips anything already manifested). Scoped to the selection only.
if [[ $FORCE -eq 1 && ${#FILES[@]} -gt 0 ]]; then
  n=0
  for f in "${FILES[@]}"; do
    rel=${f#"$RECORDINGS"/}
    m="$DETECTIONS/$rel.json"
    [[ -e "$m" ]] && { rm -f "$m"; rm -rf "$DETECTIONS/$rel.thumbs"; n=$((n + 1)); }
  done
  echo "force: cleared $n existing manifest(s)"
fi

[[ ${#FILES[@]} -eq 0 ]] && { echo "nothing to do"; exit 0; }

# Drop files that already have a manifest BEFORE handing the list to
# parallel. The worker checks this too, but only after paying a python
# start-up and an OpenCV import (~0.3s each) to reach the same conclusion —
# which on a 10k-file tree is ~25 minutes of pure skipping per sweep. A
# shell test costs a stat, so an unfiltered sweep stays cheap and there is
# no need to keep a --from window narrow enough to finish in time.
if [[ $FORCE -eq 0 ]]; then
  TODO=()
  for f in "${FILES[@]}"; do
    rel=${f#"$RECORDINGS"/}
    [[ -e "$DETECTIONS/$rel.json" ]] || TODO+=("$f")
  done
  skipped=$(( ${#FILES[@]} - ${#TODO[@]} ))
  [[ $skipped -gt 0 ]] && echo "already done: $skipped"
  FILES=("${TODO[@]}")
  [[ ${#FILES[@]} -eq 0 ]] && { echo "nothing to do"; exit 0; }
fi

if [[ $LIMIT -gt 0 && ${#FILES[@]} -gt $LIMIT ]]; then
  echo "limit: taking the newest $LIMIT of ${#FILES[@]} pending"
  FILES=("${FILES[@]:0:$LIMIT}")
fi
echo "candidate files: ${#FILES[@]}"

if [[ $DRYRUN -eq 1 ]]; then
  printf '%s\n' "${FILES[@]}"
  echo "(dry run - nothing processed)"
  exit 0
fi

# Resumability comes from the WORKER (it skips files that already have a
# manifest), not from the joblog. Parallel's --resume-failed would key on the
# command string instead, silently skipping any file it had run before - which
# also defeats --force, since clearing manifests wouldn't change the joblog.
# '+' opens the joblog in append mode so history accumulates across runs.
PAR=(parallel --jobs "$JOBS" --joblog "+$JOBLOG" --line-buffer)
if [[ -n "$SSHLOGINFILE" ]]; then
  # Same paths on every node (identical NFS mount + repo path) means no file
  # transfer is needed: each node reads its input straight off the ro mount.
  PAR+=(--sshloginfile "$SSHLOGINFILE" --workdir "$(dirname "$WORKER")")
fi

printf '%s\n' "${FILES[@]}" | "${PAR[@]}" \
  "$PYBIN" "$WORKER" --input {} \
    --input-root "$RECORDINGS" --output-root "$DETECTIONS"
