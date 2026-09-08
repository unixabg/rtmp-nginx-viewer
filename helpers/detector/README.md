# Object Detection over Recorded Files (NFS + GNU Parallel)

This describes an **optional, fully decoupled** object-detection add-on for
rtmp-nginx-viewer. It reads the recordings over a **read-only NFS mount**,
hunts through them for objects (person, car, etc.), and writes a folder of
detection events that a viewer page can browse like the history page.

Nothing here touches the recording or streaming path. If the detection box is
slow, off, or broken, **24/7 recording is unaffected** — the detector is just a
read-only consumer of files. That decoupling is the whole point.

## Architecture

```
  rtmp-nginx-viewer box                 detection box(es) / cluster
  ┌─────────────────────┐               ┌──────────────────────────┐
  │ nginx-rtmp records   │   NFS (ro)    │ mount recordings read-only│
  │ /videos/recordings   │ ────────────► │ GNU Parallel fans files   │
  │                      │               │ detect_worker.py per file │
  │ serves /detections ◄─┼───────────────┤ writes manifests+thumbs   │
  └─────────────────────┘   (rw output)  └──────────────────────────┘
```

The detector emits, per source video, a small JSON manifest plus optional
annotated thumbnails. The output lives in a **separate** writable location
(local disk or its own share), which the viewer box serves at `/detections`.

## Quickstart (Makefile)

The `Makefile` wraps everything below. `make help` lists targets and prints
the paths it will use.

```
make help                       # targets + current settings
make install                    # apt deps, venv, python packages, scripts
make model                      # fast CPU export (ACCEL=ncnn on ARM)
make doctor                     # verify install, mounts, permissions
make test                       # run the worker on one real recording
```

Detection box (defaults assume the NFS layout) versus viewer/NVR box
(`PROFILE=viewer` switches to `/videos/...` and keeps the throttling):

```
make install                                   # detection box
make install PROFILE=viewer                    # viewer box
make install GPU=1                             # CUDA wheels instead of CPU
```

Running:

```
make dry-run PROFILE=viewer CAMERA='Camera3?' FROM=2026-09-06
make run     PROFILE=viewer CAMERA=Camera32 FROM=2026-09-05 TO=2026-09-06
make run     PROFILE=viewer CAMERA=Camera32 FORCE=1
make status                                    # progress and disk use
make prune                                     # apply retention now
make install-cron PROFILE=viewer               # 10-min sweep + nightly prune
```

Removal:

```
make uninstall     # scripts + venv; keeps cameras.json and all detections
make purge         # also deletes the detections tree (prompts first)
```

Useful variables: `PREFIX` (default `/opt/detection`), `RECORDINGS`,
`DETECTIONS`, `JOBS`, `GPU`, `ACCEL`, `DETECT_MODEL`, `NICE`. `make install`
never overwrites an existing `cameras.json`, and `make upgrade` refreshes
scripts and Python packages while leaving your config and detections alone.

The sections below document what those targets do, and are also the manual
path if you'd rather not use make.

## 1. Export the recordings over NFS (read-only)

On the rtmp-nginx-viewer box, export the recordings directory read-only to the
detection box(es). Edit `/etc/exports`:

```
# /etc/exports — read-only export of recordings to the detection network
/videos/recordings   10.0.0.0/24(ro,no_subtree_check,all_squash)
```

`ro` enforces read-only at the server, so even a misbehaving client cannot
write into the recording tree. Then:

```
sudo exportfs -ra
sudo systemctl enable --now nfs-server
```

On each detection node, mount it read-only at the **same path on every node**
(GNU Parallel relies on the path being identical across the cluster):

```
sudo mkdir -p /mnt/recordings
sudo mount -t nfs -o ro,soft,timeo=30 viewer-box:/videos/recordings /mnt/recordings
```

For a permanent mount, add to `/etc/fstab` on each node:

```
viewer-box:/videos/recordings  /mnt/recordings  nfs  ro,soft,timeo=30,_netdev  0  0
```

> Use `soft` so a node doesn't hang forever if the NFS server blips; the worker
> will just fail that file and move on. Keep the output directory
> (`/var/detections`) on a **different** mount that is writable and
> viewer-readable.

## 2. Install the worker on each detection node

Use a dedicated virtualenv rather than system pip — modern Debian/Ubuntu
marks system Python as externally managed (pip refuses or needs
`--break-system-packages`), and a venv keeps the ultralytics/opencv stack
pinned and isolated from distro upgrades. Same layout on every node:

```
sudo apt install python3-venv parallel
sudo mkdir -p /opt/detection /var/detections
sudo cp detect_worker.py run_detection.sh prune_detections.sh /opt/detection/
sudo chmod +x /opt/detection/*.sh
sudo python3 -m venv /opt/detection/venv

# Default: CPU-only wheels (~250 MB). Correct for any node without an
# NVIDIA card, and the CPU-format export below (NCNN/OpenVINO) is the real CPU speedup anyway.
sudo mkdir -p /var/tmp/pip
sudo TMPDIR=/var/tmp/pip /opt/detection/venv/bin/pip install --no-cache-dir \
    torch torchvision --index-url https://download.pytorch.org/whl/cpu
sudo TMPDIR=/var/tmp/pip /opt/detection/venv/bin/pip install --no-cache-dir \
    ultralytics opencv-python-headless
sudo rm -rf /var/tmp/pip
```

> **Why the TMPDIR:** on most distros `/tmp` is a RAM-backed tmpfs capped
> well under 2 GB, and pip unpacks wheels there. The CPU wheel set fits;
> the CUDA wheel set (below) emphatically does not — cudnn alone is 550 MB
> — and fails with `OSError: [Errno 28] No space left on device` even when
> the disk has plenty of room. `/var/tmp` is on disk by convention.

**NVIDIA node instead?** Replace the two `pip install` lines with one
(keeping the TMPDIR), which pulls the default CUDA-enabled wheels (~2.5 GB):

```
sudo TMPDIR=/var/tmp/pip /opt/detection/venv/bin/pip install --no-cache-dir \
    ultralytics opencv-python-headless
```

No `source .../activate` is ever needed: `run_detection.sh` automatically
uses `/opt/detection/venv/bin/python3` when it exists (falling back to
system `python3` otherwise), which also means cron jobs — which activate
nothing — get the right interpreter for free. To upgrade later:
`sudo /opt/detection/venv/bin/pip install -U ultralytics`.

### GPU or CPU: same code, auto-detected

The worker never specifies a device. At inference time Ultralytics uses an
NVIDIA GPU if PyTorch can see one (CUDA), and falls back to CPU otherwise —
no flags, no code changes, and a mixed cluster of GPU and CPU nodes just
works. Video decode is CPU (OpenCV/ffmpeg) in both cases by design.

Per-node choices that follow from this:

* **CPU-only node:** the default install above already uses the slim CPU
  wheels. Do the export below (NCNN for ARM, OpenVINO for Intel); it's the big CPU speedup.
* **NVIDIA node:** use the CUDA install variant above and **skip the NCNN
  export** — NCNN inference is CPU-only, so pointing `DETECT_MODEL` at it
  would leave the GPU idle. Keep the default `.pt` model. Verify with:
  `/opt/detection/venv/bin/python3 -c "import torch; print(torch.cuda.is_available())"`

Since `DETECT_MODEL` is an environment variable set per node, GPU nodes run
the `.pt` while CPU nodes run their exported format, side by side in one cluster.

CPU throughput tip (measured on a Raspberry Pi 5): YOLO11n was ~400 ms per
inference in PyTorch but ~80 ms exported to NCNN — model format matters more
than raw hardware. Pick the export for your CPU family:

* **ARM (Pi, ARM SBCs):** NCNN. Install its exporter deps explicitly first —
  the auto-installer is unreliable under sudo:

  ```
  sudo TMPDIR=/var/tmp/pip /opt/detection/venv/bin/pip install --no-cache-dir ncnn pnnx
  cd /opt/detection && sudo ./venv/bin/yolo export model=yolo11n.pt format=ncnn
  export DETECT_MODEL=/opt/detection/yolo11n_ncnn_model
  ```

* **Intel (Celeron/Core/Xeon):** OpenVINO usually wins on Intel silicon:

  ```
  sudo TMPDIR=/var/tmp/pip /opt/detection/venv/bin/pip install --no-cache-dir openvino
  cd /opt/detection && sudo ./venv/bin/yolo export model=yolo11n.pt format=openvino
  export DETECT_MODEL=/opt/detection/yolo11n_openvino_model
  ```

The worker auto-detects the format from the directory `DETECT_MODEL` points
at, so mixed nodes can each use their best format. When in doubt, export
both and compare a run's `wall_time_sec`. (Ignore any per-layer "Could not
initialize NNPACK" warnings during export — PyTorch falls back cleanly on
CPUs without those instructions.)

## 2b. Per-camera detection rules (cameras.json)

Different cameras want different objects: an indoor camera only cares about
people, a parking lot wants people and vehicles, and a tree-lined driveway
needs a less twitchy motion gate. `cameras.json` (kept next to
`detect_worker.py`, or set `CAMERA_RULES=/path/to/file.json`) maps camera
name patterns to those settings.

Rules match with shell globs against the recording's **filename**, which is
where nginx-rtmp puts the camera name
(`Camera32-1788648910-20260905-175510.mp4`). The **first matching rule
wins**, so list specific patterns above general ones:

```json
{
  "rules": [
    { "pattern": "Camera1-*",  "note": "front door",
      "classes": ["person", "backpack", "suitcase"], "interval": 1.0 },
    { "pattern": "Camera3?-*", "note": "indoor 30-39",
      "classes": ["person"] },
    { "pattern": "*-parking-*", "note": "lot",
      "classes": ["person", "car", "truck", "bus"], "min_area": 4000 },
    { "pattern": "CameraTest-*", "note": "skip entirely", "classes": [] }
  ],
  "default": {
    "classes": ["person", "car", "truck", "bicycle", "motorcycle", "bus", "dog"],
    "conf": 0.45, "min_area": 1500, "interval": 2.0
  }
}
```

Per-rule keys (only `classes` is required; the rest inherit from `default`):

| key | meaning |
|---|---|
| `classes` | COCO class names to keep. `[]` skips the camera entirely. |
| `conf` | confidence threshold; raise it if a scene throws false positives |
| `min_area` | motion-gate px²; raise for cameras with trees/rain/traffic |
| `interval` | seconds between sampled frames; lower for doors and driveways |

Useful COCO classes: `person`, `bicycle`, `car`, `motorcycle`, `bus`,
`truck`, `cat`, `dog`, `bird`, `backpack`, `handbag`, `suitcase`.

### The model only knows its own vocabulary

`classes` filters what the model reports; it cannot invent categories. The
default YOLO11 models are trained on COCO's 80 classes, which cover people,
vehicles, and common domestic animals — but **not** wildlife like raccoons,
squirrels, deer, or coyotes. Asking for a class the model has never heard of
matches nothing and yields empty manifests.

The worker now warns on stderr when a configured class isn't in the model,
and `make classes` lists what the current model actually knows, flagging any
entry in `cameras.json` that will never match:

```
make classes PROFILE=viewer DETECT_MODEL=/opt/detection/yolo11n_openvino_model
```

If you want species outside COCO, in rough order of effort:

* **Use a proxy class.** COCO models usually report a raccoon as `cat`,
  `dog`, or `bear`, and a squirrel as `cat` or `bird`. Crude, but if the
  question is "did an animal come up the driveway", filtering for
  `["cat","dog","bird","bear"]` answers it without changing anything. False
  positives are the cost.
* **Open-vocabulary detection.** YOLO-World and YOLOE accept arbitrary text
  prompts — literally `["raccoon", "squirrel"]` — with no training. This is
  the direct answer, and it slots into the pluggable `Detector` class. The
  cost is speed: these models are several times heavier than YOLO11n and
  don't export to NCNN/OpenVINO as cleanly, so they want a GPU node rather
  than a Celeron.
* **A wildlife-specific model.** MegaDetector finds animals reliably but
  only labels them `animal`, not by species; pairing it with a species
  classifier is the accurate-but-involved route. Or fine-tune YOLO on
  labelled clips of your own — your recordings are the training set, and
  a few hundred examples per species goes a long way.

Notes:

* **Narrower class lists are faster** — the model runs the same, but a
  tighter `min_area` or longer `interval` on a busy camera is the real
  saving, since the motion gate is what decides how often the model runs.
* The file is **re-read per file processed**, so edits take effect on the
  next recording; nothing to restart. A missing or malformed file falls
  back to built-in defaults with a warning rather than failing the run.
* `classes: []` still writes a stub manifest so the file counts as done and
  isn't retried on every pass.
* Each manifest records the rule that was applied under `worker.rule`, so
  you can tell later why a given file was analyzed the way it was.
* Changing a rule does **not** reprocess already-done files (they have
  manifests). To re-run a camera under new rules, delete its manifests:
  `find /videos/detections -name 'Camera32-*.json' -delete`

## 3. Run it

Single box, all cores:

```
/opt/detection/run_detection.sh
```

### Targeted runs: one camera, one time range

Instead of sweeping everything, ask for a specific camera and/or window.
Selection uses the timestamp **in the filename** (`...-YYYYMMDD-HHMMSS.mp4`),
which is authoritative — mtime drifts if files are ever copied or touched.

```
# everything from one camera
./run_detection.sh --camera Camera32

# one camera, one full day  (--to is exclusive, so this is exactly Sep 5)
./run_detection.sh --camera Camera32 --from 2026-09-05 --to 2026-09-06

# an incident window, all cameras
./run_detection.sh --from '2026-09-05 17:00' --to '2026-09-05 18:30'

# preview the selection without processing anything
./run_detection.sh --camera 'Camera3?' --from 2026-09-01 --dry-run
```

| flag | meaning |
|---|---|
| `--camera` | glob against the filename. A bare name is anchored (`Camera32` → `Camera32-*`) so it can't also match `Camera320`. Pass a glob (`'Camera3?'`) for ranges. |
| `--from` | inclusive lower bound: `YYYY-MM-DD` or `'YYYY-MM-DD HH:MM[:SS]'` |
| `--to` | **exclusive** upper bound, so `--from 2026-09-05 --to 2026-09-06` is one clean day |
| `--force` | reprocess files that already have manifests — deletes them (and their thumbnails) first, scoped to the selection only |
| `--limit N` | process at most N files this run (newest first) |
| `--dry-run` | print the file list and exit |

### Keeping a scheduled sweep bounded

An unfiltered sweep with a large history is a trap: it holds the `flock` for
hours, every later tick is skipped, and fresh recordings queue behind days of
backlog. Narrowing the time window is the obvious answer but has its own
failure mode — if a run ever outlasts the window, files age past `--from` and
are **never** processed, silently.

`--limit` solves it properly. Files are ordered newest first, so a capped run
always takes fresh recordings before history:

```
/opt/detection/run_detection.sh --limit 40
```

Each run does at most 40 files; new footage is never starved, and leftover
capacity chips away at the backlog. Size the limit above your arrival rate
(cameras × files per interval) or the backlog will never shrink — if
`already done` stops growing between runs, raise `--limit` or `JOBS`.

Backfilling history stays an explicit, separate decision:

```
# deliberately work through the backlog, in a window you choose
./run_detection.sh --from 2026-09-01 --to 2026-09-03
```

Ordering uses the timestamp in the **filename**, not mtime — mtime is when
the bytes last changed, which diverges the moment a file is copied, restored
or touched.

Through `make`, `SINCE=` gives a **relative** window instead of an absolute
date, computed when the recipe runs (so it stays correct from cron):

```
make run PROFILE=viewer SINCE=1h                    # the last hour
make run PROFILE=viewer SINCE=90m CAMERA=Camera32   # last 90 minutes, one camera
make dry-run PROFILE=viewer SINCE=2d                # last two days, preview only
```

It takes shorthand (`1h`, `90m`, `2d`) or anything `date -d` understands
(`yesterday`, `"2 hours ago"`). If both `SINCE` and `FROM` are given, `SINCE`
wins; `TO` still applies.

Notes:

* Default behavior is unchanged and still resumable: without `--force`,
  files that already have manifests are skipped, so re-running a range is
  cheap and safe.
* `--force` is what you want after editing a camera's entry in
  `cameras.json` — it re-runs just that camera under the new rules.
* Files whose names carry no parseable timestamp are kept when no range is
  requested and skipped (with a count) when one is; they can't be placed in
  time.
* Targeted runs share the joblog and honor `JOBS`, `DETECT_MODEL`, and the
  rest of the environment overrides exactly like a full sweep.

Cluster: list nodes in an `--sshloginfile` (jobs-per-node/hostname):

```
# /etc/detection/nodes.txt
8/:            # ":" = the local machine, 8 jobs
4/gpu-box      # 4 jobs on gpu-box
4/pi-node-1
```

```
/opt/detection/run_detection.sh --nodes /etc/detection/nodes.txt
```

Every node needs the identical `/mnt/recordings` mount and `/opt/detection`
path; no file transfer happens because each node reads inputs straight off
the shared read-only mount.

Resumability: files already having a manifest are skipped; files newer than
~2 minutes are excluded (the recorder may still hold them); GNU Parallel's
`--joblog --resume-failed` re-runs only failures after an interruption.
Schedule it from cron under `flock` so runs never overlap:

```
*/10 * * * *  detector  flock -n /run/lock/detect.lock \
    /opt/detection/run_detection.sh >> /var/log/detection/run.log 2>&1
```

## Tracks: one object is one event

Detections are grouped into **tracks** before the manifest is written, so a
car parked in view for twenty minutes is one entry, not one per sampled
frame. Each track records when the object appeared and disappeared, how many
sightings it had, its peak confidence, and where it entered and left the
frame:

```json
{
 "event_count": 2, "moving_count": 1, "stationary_count": 1,
 "tracks": [
  { "id": 1, "label": "car", "first_seen": 0.1, "last_seen": 18.1,
    "duration": 18.0, "frames": 10, "conf_max": 0.9, "stationary": false,
    "box_first": [20,120,110,180], "box_last": [488,120,578,180],
    "thumb": "track001-car-t0s.jpg" }
 ]
}
```

**Thumbnails are named after their track** — `track001-car-t0s.jpg` is
track 1, a car, best seen at 0 s — so an image and its manifest entry are
obviously the same object. One image per track (its highest-confidence
sighting) rather than one per frame.

**`stationary`** separates a car driving past from a car sitting in the lot:
it's true when the box centre never wandered more than a quarter of the
box's own size over the track's life. Normalizing by box size is what lets
one threshold work for both a car filling the frame and a person far down a
driveway. Filter these out for "what happened" browsing, or keep them for
"what's been sitting there for three hours".

How association works: each detection is matched to an existing track of the
same label if the boxes overlap (IoU ≥ `TRACK_IOU`) **or** the detection
lands near where that track's recent velocity predicts it should be
(`TRACK_MAX_MOVE`, in box-diagonal units). The prediction step matters
because at 2-second sampling a fast car moves further than its own width
between frames, so overlap alone would fragment it into a track per frame.
A track closes after `TRACK_MAX_GAP_SEC` unseen. These constants live at the
top of `detect_worker.py`.

Known limits:

* **Sampling sets the resolution.** Something crossing the frame in under one
  `interval` may appear in a single frame. Lower `interval` for driveways
  and doors — it's a per-camera setting in `cameras.json`.
* **Segment boundaries are hard cuts.** Each file is processed
  independently, so a car parked across six segments produces six tracks,
  one per file. Collapsing those belongs in the browse layer; doing it in the
  worker would make files order-dependent and break parallelism.
* Track ids are per file, not global. `track001` in one manifest has nothing
  to do with `track001` in the next.

## 4. Serve /detections from the viewer box

Two pieces: a static index built from the manifests, and a page that reads it.
The browser can't list a directory, so `build_index.py` writes small JSON
files that nginx serves like anything else — no server-side API, no runtime
dependency added to the recorder.

```
# after a detection run, (re)build the index
/opt/detection/venv/bin/python3 /opt/detection/build_index.py \
    --detections /videos/detections

# install the page next to the detections it reads
sudo cp detections.html /videos/detections/
```

It writes `<detections>/index/days.json` plus one `YYYY-MM-DD.json` shard per
day. Sharding matters at scale: 10,000 recordings is roughly 20,000 tracks,
which is a multi-megabyte blob as one file — per-day shards keep each page
load small. Rebuilds are incremental (only days with newer manifests);
`--all` forces a full rebuild after a format change.

nginx, on the viewer box:

```nginx
location /detections/ {
    alias /videos/detections/;
    index detections.html;
    autoindex off;                 # the page reads index/*.json instead
}

location /recordings/ {
    alias /videos/recordings/;     # the page links video playback here
}
```

Then browse to `/detections/`. If your recordings are served somewhere other
than `/recordings/`, edit `REC_BASE` at the top of the script block in
`detections.html`.

### What the page does

A contact sheet: one tile per track, stamped with wall-clock time, camera,
object type, and duration. Days run down the left with their track counts.
Filter by camera and object; parked and static objects are hidden by default
and can be shown with one click. Clicking a tile opens the recording seeked
to three seconds before the track started, so the event has some lead-in.

It is **read-only by design** — it never triggers detection. Runs stay on the
command line where you can see and control what they cost.

Notes:

* Colour on each tile encodes the object family (person, vehicle, animal,
  other), so a sheet can be scanned without reading every stamp.
* Static objects appear desaturated and tagged, rather than being deleted —
  "a van has been parked there since 6am" is sometimes the thing you want.
* Thumbnails load lazily and tiles fall back to a labelled placeholder if an
  image is missing (e.g. a run made with `--no-thumbs`).
* No fonts, frameworks, or CDN calls: the page works on a box with no route
  to the internet.
* Wall-clock times come from the recording's filename plus the track offset,
  so they're real times of day, not offsets into a file.

## Measuring speed across machines

Every manifest records how long the file took and where the time went, which
is what you need when rolling this out to hardware of varying capability:

```json
"timing": {
  "wall_sec": 13.4, "video_sec": 300.0, "realtime_factor": 22.4,
  "decode_sec": 3.2, "motion_gate_sec": 0.4, "inference_sec": 8.9,
  "warmup_sec": 2.4, "thumbs_sec": 0.3,
  "frames_sampled": 150, "frames_inferred": 40, "ms_per_inference": 96.4
},
"host": { "name": "nvr-viewer", "model": "/opt/detection/yolo11n_openvino_model" }
```

`realtime_factor` is the headline number: 22.4 means a 5-minute recording
took 13 seconds, so **one worker keeps up with roughly 22 cameras** of
continuous recording. Multiply by your job count for the box's capacity.

`warmup_sec` is the first inference only, which also pays model load and
compilation — OpenVINO can spend seconds there. Because each file is its own
process, that cost is paid **once per file**, so it matters more as segments
get shorter. It is excluded from `ms_per_inference`, which would otherwise
make a quiet file with two inferences look catastrophically slow.

The decode/inference split tells you what to fix. Inference-dominated means
a faster model or a GPU will help. Decode-dominated means it won't — you're
bound by pulling frames off disk, and the lever is a longer `interval` or
fewer parallel jobs.

`make bench` summarizes across everything already processed, grouped by host
and model, so a mixed fleet can be compared directly:

```
$ make bench PROFILE=viewer
host               model                               files   wall_s xRealtime  infer_ms decode%
gpu-box            yolo11n.pt                              6      1.7     175.2      10.4   17.2%
nvr-viewer         yolo11n_openvino_model                  6     13.1      22.9      96.4   24.2%
pi5                yolo11n_ncnn_model                      6     38.1       7.9     336.8   25.2%

median 22.9x realtime -> one worker keeps up with ~22 continuous cameras
```

It reads existing manifests and costs nothing to run. Manifests written
before timing existed are counted and reported separately; re-run those
files with `--force` if you want them measured.

## 5. Retention for detections

`prune_detections.sh` (nightly cron) applies a two-tier policy: thumbnails
are removed after `THUMB_DAYS` (default 30, matching recording retention);
tiny JSON manifests are kept `MANIFEST_DAYS` (default 90) so you keep a
searchable "person on cam03 at 14:05" index even after the video is gone.
Orphaned manifests whose source video has been recycled are also cleaned up.

## 6. Latency characteristic

This pipeline is **post-hoc** by design: detection sees only files the
recorder has closed, so an event becomes browsable one segment length plus
queue time after it happens — with 5-minute segments and a 10-minute cron,
typically 5–15 minutes. The only sources are rtmp-nginx-viewer recordings;
nothing subscribes to live camera streams. To shrink the latency, shorten
the segment length in nginx and tighten the cron interval — the trade-off is
more, smaller files (more `exec_record_done` firings, more worker forks),
not more bandwidth.

## Examples / cookbook

Real invocations, copy-paste ready. Environment variables set the paths and
throttling; flags set the selection.

### On the viewer/NVR box (local files, throttled behind nginx)

Everything here overrides the NFS defaults and runs at low priority so
recording always wins. Always start with `--dry-run` to check the selection.

```
# preview: cameras 30-39, from Sep 6 onward
RECORDINGS=/videos/recordings DETECTIONS=/videos/detections \
JOBLOG=/videos/detections/.joblog JOBS=2 \
DETECT_MODEL=/opt/detection/yolo11n_openvino_model \
nice -n 15 ionice -c3 /opt/detection/run_detection.sh \
  --camera 'Camera3?' --from 2026-09-06 --dry-run

# same thing for real (drop --dry-run)
RECORDINGS=/videos/recordings DETECTIONS=/videos/detections \
JOBLOG=/videos/detections/.joblog JOBS=2 \
DETECT_MODEL=/opt/detection/yolo11n_openvino_model \
nice -n 15 ionice -c3 /opt/detection/run_detection.sh \
  --camera 'Camera3?' --from 2026-09-06
```

Rather than retype that, drop the environment into a wrapper:

```bash
# /opt/detection/env-viewer.sh   (chmod +x)
export RECORDINGS=/videos/recordings
export DETECTIONS=/videos/detections
export JOBLOG=/videos/detections/.joblog
export JOBS=2
export DETECT_MODEL=/opt/detection/yolo11n_openvino_model
exec nice -n 15 ionice -c3 /opt/detection/run_detection.sh "$@"
```

```
/opt/detection/env-viewer.sh --camera 'Camera3?' --from 2026-09-06
```

`"$@"` passes flags straight through, so every example below works with the
wrapper too. The detection box gets its own wrapper with the NFS paths and a
higher `JOBS`; the commands you type stay the same.

### Common selections

```
# one file, by hand - the smoke test
/opt/detection/venv/bin/python3 /opt/detection/detect_worker.py \
  --input /videos/recordings/Camera32-1788648910-20260905-175510.mp4 \
  --input-root /videos/recordings --output-root /tmp/detect-test

# everything from one camera, all history
./run_detection.sh --camera Camera32

# one camera, exactly one day (--to is exclusive)
./run_detection.sh --camera Camera32 --from 2026-09-05 --to 2026-09-06

# incident window across every camera
./run_detection.sh --from '2026-09-05 17:00' --to '2026-09-05 18:30'

# a numeric block of cameras since a date
./run_detection.sh --camera 'Camera1[0-9]' --from 2026-09-01

# re-run one camera after editing its rules in cameras.json
./run_detection.sh --camera Camera32 --force

# re-run just one day of one camera under new rules
./run_detection.sh --camera Camera32 --from 2026-09-05 --to 2026-09-06 --force

# full sweep, everything not yet processed
./run_detection.sh
```

`--dry-run` costs nothing (it is a `find` plus a filter, no decoding), so
run it freely against the whole tree to see what a selection covers.

### Checking on a run

```
# progress
find /videos/detections -name '*.json' | wc -l
tail -f /videos/detections/.joblog

# which files actually had detections, newest first
grep -l '"event_count": [1-9]' -r /videos/detections --include='*.json' | tail -20

# what did one file find?
python3 -m json.tool /videos/detections/Camera32-1788648910-20260905-175510.mp4.json

# every manifest mentioning a person on Sep 5
grep -l '"person"' /videos/detections/*-20260905-*.json
```

### Stopping and resuming

```
kill %1                       # backgrounded run: parallel finishes in-flight files
pkill -f run_detection.sh     # if the shell is gone
pgrep -af 'detect_worker|run_detection'   # empty output = stopped
```

Nothing is lost — completed manifests stay on disk and re-running the same
command skips them, so long backfills can be run in whatever slices suit
you. Use `tmux` or `screen` for anything expected to run for hours.

### Housekeeping

```
# nightly prune, viewer-box paths
DETECTIONS=/videos/detections RECORDINGS=/videos/recordings \
  /opt/detection/prune_detections.sh

# clear manifests for one camera so the next run redoes it
find /videos/detections -name 'Camera32-*.json' -delete
```

## Sizing reference (55 cameras, 2 Mbps, 5-min segments)

~660 files/hour, ~75 MB each → ~14 GB/h to keep pace. With 2-second frame
sampling and the motion gate, decode dominates, not inference; a single
midrange box (or one GPU node) keeps up with headroom, and gigabit NFS is
nowhere near saturated. Add nodes by adding lines to `nodes.txt`.
