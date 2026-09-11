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
make install GPU=1 CUDA_INDEX=https://download.pytorch.org/whl/cu130   # CUDA 13 driver
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
`DETECTIONS`, `JOBS`, `GPU`, `CUDA_INDEX`, `DECODE`, `DECODE_KEYFRAMES`,
`DECODE_MAX_W`, `INTERVAL_SCALE`, `ACCEL`, `DETECT_MODEL`, `NICE`. `make install`
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

**NVIDIA node instead?** Keep both `pip install` lines but point the
torch one at a CUDA wheel index that matches your driver (~2.5 GB):

```
sudo TMPDIR=/var/tmp/pip /opt/detection/venv/bin/pip install --no-cache-dir \
    torch torchvision --index-url https://download.pytorch.org/whl/cu126
sudo TMPDIR=/var/tmp/pip /opt/detection/venv/bin/pip install --no-cache-dir \
    ultralytics opencv-python-headless
```

> **Why pin the index:** a bare `pip install torch` now gives you a CUDA 13
> build, which needs a CUDA 13 driver (roughly 580 and up). Debian stable
> ships the 550 driver (CUDA 12.4) and that build will not load on it —
> `torch.cuda.is_available()` just returns `False` with no useful error,
> after a 2.5 GB download. The `cu126` wheels run on any CUDA 12.x driver, so
> they are the default (`CUDA_INDEX` in the Makefile). Check what you have
> with `nvidia-smi`: the "CUDA Version" in the header is the ceiling for the
> torch build. 12.x → `cu126`; 13.x → `cu130`. `make doctor` prints the
> driver, the CUDA build torch was compiled against, and whether they agree.

Installing torch first also means the later `ultralytics` install leaves it
alone — pip only replaces a dependency when the one present does not satisfy
the requirement. `make upgrade` re-pins the same index for the same reason.

**Driver notes for Debian.** `nvidia-driver` lives in `non-free`, which the
installer does not enable — add `contrib non-free` to the `deb` lines in
`/etc/apt/sources.list` (or the `Components:` lines in
`/etc/apt/sources.list.d/*.sources`) and `apt update`. Then:

```
sudo apt install linux-headers-amd64 nvidia-driver firmware-misc-nonfree \
    nvidia-smi libcuda1
```

On a headless box `--no-install-recommends` on `nvidia-driver` keeps Xorg
out, but then `nvidia-smi` and `libcuda1` must be named explicitly as above
— without `libcuda1`, `nvidia-smi` shows `CUDA Version: N/A` and torch can
never see the card. Make sure the running kernel is the newest installed one
(`uname -r` vs `apt list --installed 'linux-image-*'`) before installing, or
DKMS builds the module for a kernel you are not booted into. Check
`dkms status` says `installed` before rebooting. No CUDA toolkit is needed
from apt or NVIDIA: the torch wheels bundle their own runtime and cuDNN.

No `source .../activate` is ever needed: `run_detection.sh` automatically
uses `/opt/detection/venv/bin/python3` when it exists (falling back to
system `python3` otherwise), which also means cron jobs — which activate
nothing — get the right interpreter for free. To upgrade later:
`sudo /opt/detection/venv/bin/pip install -U ultralytics`.

### GPU or CPU: same code, auto-detected

The worker never specifies a device. At inference time Ultralytics uses an
NVIDIA GPU if PyTorch can see one (CUDA), and falls back to CPU otherwise —
no flags, no code changes, and a mixed cluster of GPU and CPU nodes just
works. Video decode is keyframe-only where the sample interval allows it,
otherwise software (`ffmpeg`, or OpenCV when ffmpeg is absent); the card's
NVDEC engine is available with `DECODE=nvdec` but measured no faster —
see [NVDEC](#nvdec-hardware-decode-on-gpu-nodes-opt-in) below.

Per-node choices that follow from this:

* **CPU-only node:** the default install above already uses the slim CPU
  wheels. Do the export below (NCNN for ARM, OpenVINO for Intel); it's the big CPU speedup.
* **NVIDIA node:** use the CUDA install variant above and **skip the NCNN
  export** — NCNN inference is CPU-only, so pointing `DETECT_MODEL` at it
  would leave the GPU idle. Keep the default `.pt` model. Verify with
  `make doctor` (the `nvidia driver:` and `torch:` lines) or directly:
  `/opt/detection/venv/bin/python3 -c "import torch; print(torch.__version__, torch.cuda.is_available())"`
  — expect a `+cu126`-style version and `True`. A `+cu130` version with
  `False` means the torch build is newer than the driver; reinstall with
  the `cu126` index.

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

### NVDEC: hardware decode on GPU nodes (opt-in)

A fast model exposes the next bottleneck. Measured on an i7-4770 with an
RTX 3070: inference dropped to 10.7 ms per frame, but a 5-minute 1440p
segment still took 15 s, of which 12.5 s was the CPU decoding H.264. Six
workers in parallel made it worse, not better — four physical cores
fighting over decode threads pushed per-file time to 62 s while the GPU sat
at 0 %. On that box the card was a fast model attached to a slow decoder.

The obvious fix is to decode on the card. Every NVIDIA GPU since 2012
carries an NVDEC block — a fixed-function H.264/HEVC decoder separate from
the CUDA cores — and the worker can use it through ffmpeg's
`h264_cuvid`/`hevc_cuvid` decoders with `DECODE=nvdec`. It is **not** the
default, for a measured reason:

**On a consumer card it was no faster than the CPU.** With 2560×1440
25 fps H.264 recordings, `DECODE=nvdec` decoded a 5-minute segment in
12.98 s against the CPU's 12.47 s, and with four workers `nvidia-smi dmon`
showed the `dec` engine pinned at 100 %. A consumer card has one NVDEC
block, and at 1440p it delivers roughly 580 frames/s *in total*, shared
across every worker: about 23× realtime for the whole box, less than four
CPU cores decoding in software. Over 80 files under cron it came out at
64.5 s per file to the CPU's 62.2 s. NVDEC is a separate engine from the
CUDA cores, so it does not slow inference — but on this hardware and
resolution it is a fifth decoder of similar speed, only a win if the CPU
has no cores to spare.

The real saving turned out to be not decoding most frames at all — see
"Sampling density and decode cost" under 2b. `DECODE=auto` (the default)
therefore picks, per file:

1. **keyframes** — if the sample interval is at least the file's GOP
   (`DECODE_KEYFRAMES`); software, and by far the cheapest;
2. **ffmpeg** — software decode, one thread per worker (`DECODE_THREADS`),
   with sampling inside ffmpeg (`-vf fps=1/interval`) so only sampled
   frames cross into Python, and decode overlapping inference in its own
   process;
3. **cv2** — the original OpenCV loop, when there is no `ffmpeg` binary.

A mixed cluster therefore does the right thing per node without per-node
settings. Force a decoder with `DECODE=nvdec|ffmpeg|cv2` (env, or
`make run DECODE=...`). `nvdec` fails the file rather than falling back,
which is what you want when verifying a node; note that keyframe mode is
chosen before the decoder, so to exercise the hardware path on a camera
whose interval allows keyframes, pair it with `DECODE_KEYFRAMES=0`.

Requirements for `DECODE=nvdec`, all from apt:

```
sudo apt install ffmpeg libnvcuvid1
```

Debian's `ffmpeg` is built with the NVIDIA codec headers, so the `*_cuvid`
decoders are present; `libnvcuvid1` (non-free, from the same driver series
as `nvidia-driver`) is the userspace library they load at runtime. Without
it the decoders are listed but cannot start. `make doctor` checks both,
then decodes a one-second test clip through `h264_cuvid` so "nvdec: ok"
means it really works on this driver — and reports it as available, not as
something `auto` will use.

Every manifest records `timing.decoder`, and `make bench` groups on it.
The same box, same recordings, every decoder tried in one day (four
workers unless noted):

```
host      model        version   decode     files  wall_s xRealtime  infer_ms  decode%
busbarn   yolo11n.pt   v1.0.3-78 cv2           80    62.2       4.8      38.2    92.4%   (six workers)
busbarn   yolo11n.pt   v1.0.3-79 ffmpeg        32    47.3       6.4      22.2    86.8%
busbarn   yolo11n.pt   v1.0.3-79 nvdec         80    64.5       4.7      17.4    91.4%
busbarn   yolo11n.pt   v1.0.3-81 keyframes     19     9.9      30.5      17.3    59.7%
```

`ffmpeg` beats `cv2` because sampling happens in ffmpeg and decode
overlaps inference; `nvdec` doesn't beat either; `keyframes` is the one
that moves the needle — 30× realtime per worker, ~120× for the box, with
decode below 60 % of wall time for the first time.

Notes:

* NVDEC throughput scales with resolution: 1080p streams would get
  roughly 1.8× the frame rate above, and the newer decoders on 40/50-series
  cards and dual-NVDEC datacenter parts change the math again. Measure
  with `nvidia-smi dmon -s u` (`dec` column) and `make bench` before
  putting `DECODE=nvdec` in a cron entry.
* Where NVDEC can still make sense: cameras opted into sub-GOP sampling
  (`gpu_interval`), which need a full decode, on a node whose CPU is the
  scarcer resource. That is a per-node judgement; `bench` is the judge.
* The sampled frames are copied to system memory (about 11 MB each at
  1440p BGR), but at a frame every half second that is noise; the `sm`
  load visible during NVDEC runs is `cuvid` moving frames it then discards.
* There is no NVDEC path for the motion gate or thumbnails; they stay on
  the CPU and are small.
* Nodes without a GPU are unaffected: `auto` lands on `keyframes`,
  `ffmpeg`, or `cv2`, and the `ffmpeg`/`cv2` paths produce identical
  manifests to before. `cv2` remains the choice for a box where installing
  `ffmpeg` is not wanted.
* **NVDEC cannot be combined with keyframe skipping.** `h264_cuvid`
  ignores `-skip_frame nokey` and decodes every frame: on a 5-minute
  1440p segment, 36 s through cuvid against 3.9 s for the software
  decoder that honours the flag. There is no configuration in which
  hardware decode wins on this hardware.

### Decode resolution

The second half of decode cost is not the decode: it is converting each
frame to BGR24 and pushing ~11 MB through a pipe, at a resolution nothing
downstream uses. The model runs at 640×640 and the motion gate at 480
wide, so a 2560×1440 frame is carried at full size only to be shrunk
twice.

`DECODE_MAX_W` (default 1280) scales inside ffmpeg, before the colour
conversion. Measured on a 60 s 1440p clip, decode-and-convert:

```
2560 wide (source)   6.47 s
1280 wide            1.33 s
 960 wide            0.96 s
```

Boxes are scaled back to the recording's own coordinates before they
reach the manifest, so detections stay comparable across nodes and
settings; `timing.decode_width` records what was actually decoded.
Thumbnails are annotated and cropped from the scaled frame — they are
640 wide in the contact sheet regardless, so there is nothing to lose
until `DECODE_MAX_W` drops below that.

Set `DECODE_MAX_W=0` to decode at source resolution. The floor worth
using is around 960: below that, small or distant objects start to fall
under the model's effective resolution and detections are lost, which
`bench` cannot see and only a `--force` re-run of a known-busy camera
will reveal. `cv2` ignores this setting — OpenCV decodes at source
resolution and scaling afterwards saves nothing.


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
      "classes": ["person", "backpack", "suitcase"], "interval": 1.0,
      "gpu_interval": 0.25 },
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
| `gpu_interval` | interval used instead on a node with a CUDA GPU; opts this camera into dense sampling at the cost of a full decode (see below) |
| `keepalive` | seconds between forced looks in a still scene (default 30, 0 = off) |

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

### Sampling density and decode cost

Two facts decide how much a file costs to analyze:

1. **An H.264 frame depends on the ones before it**, so a decoder normally
   has to decode every frame to reach the sampled one — 50 decodes per
   sample at 25 fps and a 2 s interval. That is where a CPU node spends
   ~90 % of its time, and a GPU node too: on the i7-4770 / RTX 3070 box
   inference was 0.3 s of a 15 s file.
2. **Keyframes don't.** They are decoded standalone, and cameras emit one
   every GOP (the box above: exactly 2 s). If the sample interval is at
   least the GOP, every sample can be a keyframe and `ffmpeg -skip_frame
   nokey` parses-and-discards the frames between without decoding them.
   Measured: 0.9 s instead of 9.9 s for a 60 s 1440p clip, identical
   samples and timestamps.

So the default on every node is **keyframe-only decode** whenever the
interval allows it. `DECODE_KEYFRAMES=auto` probes each file's GOP (first
20 s, cheap) and uses keyframe mode when `interval ≥ 0.9 × GOP`; sample
times snap to the actual keyframe timestamps. `DECODE_KEYFRAMES=1` forces
it (raising the interval to the GOP if needed); `0` disables it. It is a
software path — decoding one frame per GOP is negligible anywhere, so
NVDEC is never used for it.

Sampling *below* the GOP is the opposite trade: it buys time resolution
(a car crossing the frame in under 2 s) for a full decode of the file,
about 10× the cost. That is worth it on a GPU node — inference is ~10 ms,
so the marginal cost of a sample is the motion gate — but only for cameras
where it matters, so it is opt-in per camera:

* `gpu_interval` on a rule (or in `default`) is used verbatim when the
  worker has CUDA; CPU nodes ignore it and use `interval`.
* `INTERVAL_SCALE` multiplies the interval of cameras without a
  `gpu_interval`: `auto` (default) is `GPU_INTERVAL_SCALE` on CUDA nodes,
  which itself defaults to 1.0 — so nothing changes unless you ask. Set
  `GPU_INTERVAL_SCALE=0.25` on a GPU node to densify every camera (and
  accept full decode on all of them), or `INTERVAL_SCALE=<n>` to force a
  scale on any node. Floor is 0.1 s.

The manifest records `timing.decoder` (`keyframes`, `ffmpeg`, `nvdec`,
`cv2`) and `worker.interval_from` (`rule`, `gpu_interval`, `scale=N`), so
a dense GPU sample and a keyframe sample of the same camera can be told
apart in a mixed cluster, and `make bench` groups on the decoder. On the
GPU box, `bench` therefore shows the cost of each choice side by side.

Notes:

* **Narrower class lists are faster** — the model runs the same, but a
  tighter `min_area` or longer `interval` on a busy camera is the real
  saving, since the motion gate is what decides how often the model runs.
* The file is **re-read per file processed**, so edits take effect on the
  next recording; nothing to restart. A missing or malformed file falls
  back to built-in defaults with a warning rather than failing the run.
* `classes: []` still writes a stub manifest so the file counts as done and
  isn't retried on every pass.
* **Failures are bounded.** A file that can't be opened, or a detector
  crash mid-file, is retried on the next two sweeps and then written off
  with an error manifest (`"error": ...`) so the sweep stops touching it.
  Without this a single corrupt recording would be retried every ten
  minutes for its whole retention life. `make status` shows the counts;
  `--force` clears a written-off file if you want to retry after fixing
  the cause.
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
    /opt/detection/run_detection.sh >> /var/log/detection.log 2>&1
```

`make install-cron` writes this for you, along with a nightly prune and a
logrotate rule at `/etc/logrotate.d/detection` (daily, 7 kept, compressed).

The rotation uses **copytruncate rather than create**: cron holds the log
open through `>>` for the whole run, so renaming the file would send writes
to the rotated copy and lose any run in progress at rotation time.

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

**`keepalive`** exists because of a subtlety: in a dead-still scene — a
garage, an empty lot at night — the motion gate suppresses everything, so a
parked car is detected once and can never be judged stationary, since one
position shows no displacement. Every `keepalive` seconds the worker looks
anyway, so persistent objects accumulate sightings and get classified
correctly. Cost is bounded at video-length ÷ keepalive extra inferences —
ten per 5-minute recording at the default.

**`stationary`** separates a car driving past from a car sitting in the lot:
it's true when the 80th-percentile distance from the track's median position
stays under a quarter of the box's own size. Percentile and median rather
than maximum and first sighting: a detector will occasionally return one
shifted or resized box for a perfectly still object, and a max-based test
lets that single frame flip the whole track to "moving". Normalizing by box size is what lets
one threshold work for both a car filling the frame and a person far down a
driveway. Filter these out for "what happened" browsing, or keep them for
"what's been sitting there for three hours".

How association works: each detection is matched to an existing track of the
same label if the boxes overlap (IoU ≥ `TRACK_IOU`) **or** the detection
lands near where that track's recent velocity predicts it should be
(`TRACK_MAX_MOVE`, in box-diagonal units). The prediction step matters
because at 2-second sampling a fast car moves further than its own width
between frames, so overlap alone would fragment it into a track per frame.
A track closes after `TRACK_MAX_GAP_OBS` consecutive **inferred** frames
without a match (plus an absolute `TRACK_MAX_GAP_SEC` cap so unrelated
objects never merge). Counting observations rather than seconds matters
because the gate can suppress inference for minutes: a gated frame is not
evidence that an object left, so it must not age a track out. These constants live at the
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

## Knowing what's running

There is no hand-maintained version number. `make install` and `make
upgrade` stamp `git describe --tags --always --dirty` into
`/opt/detection/VERSION`, and from there it flows into:

* every manifest, as `worker.version`
* every run's start banner in the log: `=== run started ... | detector
  v1.0-3-gabc123 | host nvr-viewer ===`
* `make doctor`, which also warns when the installed version differs from
  the checkout you're standing in
* `make bench`, as a column, so a fleet comparison shows which code each
  box ran

`-dirty` means uncommitted local edits were installed. Tag releases
(`git tag v1.0`) and the number becomes readable on its own; between tags
it's `v1.0-3-gabc123` — three commits past v1.0 at that hash. When someone
reports a problem, the first line of their log answers "what are you
running" without a file diff.

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
bound by pulling frames off disk. The lever on every node is keyframe-only
decode (check `timing.decoder` says `keyframes`; if it says `ffmpeg` the
camera's interval is below its GOP); after that, one job per physical core
with `DECODE_THREADS=1`. A longer `interval` does *not* reduce decode
unless it crosses the GOP, since every H.264 frame must be decoded to
reach the next one — and NVDEC measured no faster than the CPU (see 2).

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
Orphaned manifests whose source video has been recycled are also cleaned up,
along with failure markers and an over-long joblog.

Both values are Makefile variables, so they reach the manual run *and* the
generated cron entry — no hand-editing a generated file:

```
make prune PROFILE=viewer THUMB_DAYS=7 MANIFEST_DAYS=10
make install-cron PROFILE=viewer THUMB_DAYS=7 MANIFEST_DAYS=10
```

`PRUNE_AT` sets when it runs (default `30 2`, i.e. 02:30). Don't add a
second cron file for pruning: `install-cron` already writes the entry, and
a hand-written one would both double up and be overwritten on the next
install.

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
