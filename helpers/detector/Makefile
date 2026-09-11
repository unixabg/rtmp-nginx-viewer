# Makefile for the rtmp-nginx-viewer detection add-on.
#
#   make help                  list every target
#   make install               venv + deps + scripts -> $(PREFIX)
#   make model                 export a fast CPU model (or skip on GPU nodes)
#   make doctor                check the install and answer "why isn't it working"
#   make upgrade               refresh scripts and Python deps in place
#   make uninstall             remove the install (keeps detections by default)
#
# Every path and tunable is overridable on the command line, e.g.
#   make install PREFIX=/srv/detection
#   make run CAMERA=Camera32 FROM=2026-09-05 TO=2026-09-06
#   make install-cron RECORDINGS=/videos/recordings DETECTIONS=/videos/detections
#
# The viewer/NVR box and the detection box differ only in these variables,
# so the same Makefile serves both. See PROFILE below for a shortcut.

SHELL       := /bin/bash
.DEFAULT_GOAL := help

PREFIX      ?= /opt/detection
VENV        ?= $(PREFIX)/venv
PY          := $(VENV)/bin/python3
PIP         := $(VENV)/bin/pip
TMPDIR_PIP  ?= /var/tmp/pip

# Where recordings live and detections go. Defaults are the detection-box
# (NFS) layout; override for the viewer box.
RECORDINGS  ?= /mnt/recordings
DETECTIONS  ?= /var/detections
JOBS        ?= 2
NICE        ?= nice -n 15 ionice -c3

# GPU=1 installs CUDA-enabled torch; default is the slim CPU wheel set.
CRON_LIMIT  ?= 40    # max files per scheduled sweep; newest first
LOGFILE     ?= /var/log/detection.log
# Retention for the detections tree. Thumbnails are the bulky part, so they
# usually expire with the recordings; the tiny JSON manifests are kept longer
# as a searchable index of what happened.
THUMB_DAYS    ?= 30
MANIFEST_DAYS ?= 90
# When the nightly prune runs: "minute hour". Keep the comment off this
# line — make would include the trailing spaces in the value.
PRUNE_AT      ?= 30 2
GPU         ?= 0
# CUDA_INDEX is the PyTorch wheel index used when GPU=1. PyPI's default
# torch is now a CUDA 13 build, which needs a CUDA 13 driver (~580+);
# Debian stable ships 550 (CUDA 12.4), which cannot load it. cu126 works on
# any CUDA 12.x driver, so it is the safe default for Debian. On a newer
# driver, pass CUDA_INDEX=https://download.pytorch.org/whl/cu130. Match it
# to the "CUDA Version" that nvidia-smi prints.
CUDA_INDEX  ?= https://download.pytorch.org/whl/cu126
# DECODE picks the frame decoder: auto (NVDEC if present, else ffmpeg, else
# cv2), nvdec, ffmpeg, cv2. Empty = worker default (auto). On a GPU node the
# decoder, not the model, is the bottleneck - see README "NVDEC".
DECODE      ?=
# DECODE_KEYFRAMES: auto (keyframe-only decode when interval >= GOP), 1, 0.
# INTERVAL_SCALE multiplies every camera's sample interval. Both empty =
# worker defaults. See README "Sampling density and decode cost".
DECODE_KEYFRAMES ?=
INTERVAL_SCALE ?=
# ACCEL picks the CPU export format: openvino (Intel) or ncnn (ARM).
ACCEL       ?= openvino
MODEL       ?= yolo11n.pt

# PROFILE=viewer flips the paths to the local-recordings layout in one word.
ifeq ($(PROFILE),viewer)
RECORDINGS  := /videos/recordings
DETECTIONS  := /videos/detections
endif

JOBLOG      := $(DETECTIONS)/.joblog
RUNNER      := $(PREFIX)/run_detection.sh
RUNENV      := RECORDINGS=$(RECORDINGS) DETECTIONS=$(DETECTIONS) \
               JOBLOG=$(JOBLOG) JOBS=$(JOBS) $(if $(DETECT_MODEL),DETECT_MODEL=$(DETECT_MODEL),) \
               $(if $(DECODE),DECODE=$(DECODE),) $(if $(DECODE_THREADS),DECODE_THREADS=$(DECODE_THREADS),) \
               $(if $(INTERVAL_SCALE),INTERVAL_SCALE=$(INTERVAL_SCALE),) \
               $(if $(DECODE_KEYFRAMES),DECODE_KEYFRAMES=$(DECODE_KEYFRAMES),)

SCRIPTS     := detect_worker.py run_detection.sh prune_detections.sh build_index.py
# Version is whatever git says about this checkout, stamped into
# $(PREFIX)/VERSION at install time. Never hand-maintained: a stale number
# is worse than none. "-dirty" means local uncommitted edits were installed.
VERSION     := $(shell git describe --tags --always --dirty 2>/dev/null || echo unknown)
WEBPAGE     := detections.html
CONFIG      := cameras.json

.PHONY: help install install-deps install-scripts install-config model \
        upgrade upgrade-scripts upgrade-deps upgrade-web uninstall purge \
        doctor status run dry-run prune index web classes bench \
        install-cron install-logrotate uninstall-cron test clean

## help: show this list
help:
	@echo "detection add-on — targets:"; echo
	@grep -E '^## ' $(MAKEFILE_LIST) | sed 's/^## //' | \
	    awk -F': ' '{printf "  %-18s %s\n", $$1, $$2}'
	@echo
	@echo "current settings (override on the command line):"
	@printf "  %-14s %s\n" PREFIX $(PREFIX) RECORDINGS $(RECORDINGS) \
	    DETECTIONS $(DETECTIONS) JOBS $(JOBS) GPU $(GPU) ACCEL $(ACCEL) \
	    CUDA_INDEX $(CUDA_INDEX) DECODE "$(if $(DECODE),$(DECODE),auto (worker default))" \
	    THUMB_DAYS $(THUMB_DAYS) MANIFEST_DAYS $(MANIFEST_DAYS)
	@echo
	@echo "  tip: PROFILE=viewer switches to /videos paths"

## install: full setup — apt deps, venv, Python packages, scripts, config
install: install-deps install-scripts install-config
	@echo
	@echo "installed to $(PREFIX)."
	@echo "next: make model      (fast CPU model; skip if GPU=1)"
	@echo "      make doctor     (verify)"

## install-deps: apt packages + venv + Python wheels
install-deps:
	@echo "==> apt packages"
	sudo apt-get install -y python3-venv parallel ffmpeg
ifeq ($(GPU),1)
	@echo "==> libnvcuvid1 (NVDEC userspace library, non-free; only used with DECODE=nvdec)"
	sudo apt-get install -y libnvcuvid1
endif
	@echo "==> venv at $(VENV)"
	sudo mkdir -p $(PREFIX) $(DETECTIONS) $(TMPDIR_PIP)
	[ -x $(PY) ] || sudo python3 -m venv $(VENV)
ifeq ($(GPU),1)
	@echo "==> torch with CUDA (GPU=1) from $(CUDA_INDEX)"
	sudo TMPDIR=$(TMPDIR_PIP) $(PIP) install --no-cache-dir -U \
	    torch torchvision --index-url $(CUDA_INDEX)
	sudo TMPDIR=$(TMPDIR_PIP) $(PIP) install --no-cache-dir -U \
	    ultralytics opencv-python-headless
else
	@echo "==> torch CPU-only wheels (GPU=0); set GPU=1 for CUDA"
	sudo TMPDIR=$(TMPDIR_PIP) $(PIP) install --no-cache-dir -U \
	    torch torchvision --index-url https://download.pytorch.org/whl/cpu
	sudo TMPDIR=$(TMPDIR_PIP) $(PIP) install --no-cache-dir -U \
	    ultralytics opencv-python-headless
endif
	sudo rm -rf $(TMPDIR_PIP)

## install-scripts: copy worker/runner/pruner into PREFIX
install-scripts:
	@echo "==> scripts -> $(PREFIX)  (version $(VERSION))"
	sudo mkdir -p $(PREFIX)
	sudo cp $(SCRIPTS) $(PREFIX)/
	sudo chmod +x $(PREFIX)/*.sh
	@echo "$(VERSION)" | sudo tee $(PREFIX)/VERSION >/dev/null

## install-config: install cameras.json only if absent (never clobbers yours)
install-config:
	@if [ -e $(PREFIX)/$(CONFIG) ]; then \
	    echo "==> $(PREFIX)/$(CONFIG) exists, leaving it alone"; \
	    echo "    (compare with: diff $(CONFIG) $(PREFIX)/$(CONFIG))"; \
	else \
	    echo "==> installing default $(CONFIG)"; \
	    sudo cp $(CONFIG) $(PREFIX)/; \
	fi

## model: export a fast CPU model (ACCEL=openvino for Intel, ncnn for ARM)
model:
ifeq ($(GPU),1)
	@echo "GPU=1: skipping CPU export — keep the .pt so CUDA is used."
else
	@echo "==> exporting $(MODEL) to $(ACCEL)"
ifeq ($(ACCEL),ncnn)
	sudo TMPDIR=$(TMPDIR_PIP) $(PIP) install --no-cache-dir ncnn pnnx
else
	sudo TMPDIR=$(TMPDIR_PIP) $(PIP) install --no-cache-dir openvino
endif
	cd $(PREFIX) && sudo $(VENV)/bin/yolo export model=$(MODEL) format=$(ACCEL)
	@echo
	@echo "export done. use it with:"
	@echo "  DETECT_MODEL=$(PREFIX)/$(basename $(MODEL))_$(ACCEL)_model"
endif

## upgrade: refresh scripts, page and Python packages in place (config kept)
upgrade: upgrade-scripts upgrade-deps upgrade-web
	@echo
	@echo "upgrade complete — run 'make doctor' to verify."

## upgrade-scripts: copy newer scripts over the installed ones
upgrade-scripts:
	@echo "==> refreshing scripts in $(PREFIX)  (version $(VERSION))"
	sudo cp $(SCRIPTS) $(PREFIX)/
	sudo chmod +x $(PREFIX)/*.sh
	@echo "$(VERSION)" | sudo tee $(PREFIX)/VERSION >/dev/null
	@echo "    cameras.json left as-is; diff it yourself if the format changed:"
	@diff -q $(CONFIG) $(PREFIX)/$(CONFIG) >/dev/null 2>&1 \
	    && echo "    (identical)" || echo "    (differs — expected if you customized it)"

## upgrade-web: refresh the browse page if it is already installed
upgrade-web:
	@if [ -e $(DETECTIONS)/$(WEBPAGE) ]; then \
	    echo "==> refreshing $(DETECTIONS)/$(WEBPAGE)"; \
	    sudo cp $(WEBPAGE) $(DETECTIONS)/; \
	    $(PY) $(PREFIX)/build_index.py --detections $(DETECTIONS) || true; \
	else \
	    echo "==> browse page not installed here; 'make web' to add it"; \
	fi

## upgrade-deps: pull newer ultralytics/opencv into the venv
upgrade-deps:
	@echo "==> upgrading Python packages"
	sudo mkdir -p $(TMPDIR_PIP)
ifeq ($(GPU),1)
	@echo "    (GPU=1: keeping torch on $(CUDA_INDEX) so an upgrade can't"
	@echo "     silently pull a CUDA build the installed driver can't load)"
	sudo TMPDIR=$(TMPDIR_PIP) $(PIP) install --no-cache-dir -U \
	    torch torchvision --index-url $(CUDA_INDEX)
	sudo TMPDIR=$(TMPDIR_PIP) $(PIP) install --no-cache-dir -U \
	    ultralytics opencv-python-headless
else
	@echo "    (GPU=0: keeping torch on the CPU index so an upgrade can't"
	@echo "     silently pull ~2.5 GB of CUDA wheels onto a CPU-only box)"
	sudo TMPDIR=$(TMPDIR_PIP) $(PIP) install --no-cache-dir -U \
	    torch torchvision --index-url https://download.pytorch.org/whl/cpu
	sudo TMPDIR=$(TMPDIR_PIP) $(PIP) install --no-cache-dir -U \
	    ultralytics opencv-python-headless
endif
	sudo rm -rf $(TMPDIR_PIP)

## doctor: check install, mounts, model, and permissions
doctor:
	@echo "=== detection add-on doctor ==="
	@printf "%-22s" "python venv:";      [ -x $(PY) ] && echo "ok  ($(PY))" || echo "MISSING — run 'make install'"
	@printf "%-22s" "gnu parallel:";     command -v parallel >/dev/null && echo "ok" || echo "MISSING — apt install parallel"
	@printf "%-22s" "scripts:";          [ -x $(RUNNER) ] && echo "ok  ($(PREFIX))" || echo "MISSING — run 'make install-scripts'"
	@printf "%-22s" "installed version:"; v=$$(cat $(PREFIX)/VERSION 2>/dev/null || echo none); \
	    if [ "$$v" = "$(VERSION)" ]; then echo "$$v"; \
	    else echo "$$v  (checkout is $(VERSION) — run 'make upgrade')"; fi
	@printf "%-22s" "cameras.json:"; \
	  p=$$([ -x $(PY) ] && echo $(PY) || command -v python3); \
	  if [ ! -e $(PREFIX)/$(CONFIG) ]; then echo "absent (defaults will be used)"; \
	  elif [ -z "$$p" ]; then echo "present (no python available to validate)"; \
	  elif $$p -c "import json;json.load(open('$(PREFIX)/$(CONFIG)'))" 2>/dev/null; then \
	    echo "ok  (valid json, $$($$p -c "import json;print(len(json.load(open('$(PREFIX)/$(CONFIG)')).get('rules',[])))") rule(s))"; \
	  else echo "INVALID JSON — worker will fall back to defaults"; fi
	@printf "%-22s" "nvidia driver:"; \
	  if command -v nvidia-smi >/dev/null; then \
	    d=$$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1); \
	    c=$$(nvidia-smi 2>/dev/null | sed -n 's/.*CUDA Version: *\([0-9.N/A]*\).*/\1/p' | head -1); \
	    if [ -z "$$d" ]; then echo "nvidia-smi present but no GPU answered (module not loaded? nouveau still bound?)"; \
	    elif [ "$$c" = "N/A" ] || [ -z "$$c" ]; then echo "$$d  CUDA N/A — apt install libcuda1 (torch needs libcuda.so.1)"; \
	    else m=$${c%%.*}; case $$m in 12) i=cu126;; 13) i=cu130;; *) i=cu$${m}x;; esac; \
	    echo "$$d  (CUDA $$c) — torch must be a CUDA $$m.x build (CUDA_INDEX=https://download.pytorch.org/whl/$$i)"; fi; \
	  else echo "none (nvidia-smi not found; fine for a CPU node)"; fi
	@printf "%-22s" "torch:"; \
	  $(PY) -c "import torch;print('ok  ',torch.__version__,'built for cuda',torch.version.cuda,'| cuda available',torch.cuda.is_available())" 2>/dev/null || echo "MISSING"
	@if [ "$(GPU)" = "1" ] && [ -x $(PY) ] && ! $(PY) -c "import torch,sys;sys.exit(0 if torch.cuda.is_available() else 1)" 2>/dev/null; then \
	    echo "                      ^ GPU=1 but torch can't see the card. Usual causes: torch built for a newer"; \
	    echo "                        CUDA than the driver (reinstall with CUDA_INDEX=.../cu126), libcuda1 missing,"; \
	    echo "                        or the nvidia module not loaded (reboot after driver install)."; fi
	@printf "%-22s" "opencv:";           $(PY) -c "import cv2;print('ok  ',cv2.__version__)" 2>/dev/null || echo "MISSING"
	@printf "%-22s" "ffmpeg:"; \
	  if command -v ffmpeg >/dev/null && command -v ffprobe >/dev/null; then \
	    echo "ok  ($$(ffmpeg -version 2>/dev/null | head -1 | cut -d' ' -f3))"; \
	  else echo "MISSING — apt install ffmpeg (worker falls back to cv2 decode)"; fi
	@printf "%-22s" "nvdec:"; \
	  if ! command -v ffmpeg >/dev/null; then echo "n/a (no ffmpeg)"; \
	  elif [ ! -e /dev/nvidiactl ]; then echo "n/a (no NVIDIA device)"; \
	  elif ! ffmpeg -hide_banner -decoders 2>/dev/null | grep -q h264_cuvid; then \
	    echo "UNAVAILABLE — this ffmpeg has no *_cuvid decoders"; \
	  elif ! ldconfig -p 2>/dev/null | grep -q libnvcuvid.so.1; then \
	    echo "UNAVAILABLE — apt install libnvcuvid1"; \
	  elif ffmpeg -hide_banner -loglevel error -f lavfi -i testsrc2=size=64x64:rate=5:duration=1 -c:v libx264 -pix_fmt yuv420p -f mp4 -y /tmp/.nvdec-probe.mp4 2>/dev/null \
	       && ffmpeg -hide_banner -loglevel error -c:v h264_cuvid -i /tmp/.nvdec-probe.mp4 -f null - 2>/dev/null; then \
	    rm -f /tmp/.nvdec-probe.mp4; echo "ok  (h264_cuvid decoded a test clip; available with DECODE=nvdec)"; \
	  else rm -f /tmp/.nvdec-probe.mp4; echo "FAILED — h264_cuvid present but could not decode (driver/libnvcuvid mismatch?)"; fi
	@printf "%-22s" "keyframes:"; \
	  f=$$(find $(RECORDINGS) -maxdepth 1 -name '*.mp4' -mmin +2 2>/dev/null | head -1); \
	  if [ -z "$$f" ]; then echo "n/a (no recording to probe)"; \
	  elif ! command -v ffprobe >/dev/null; then echo "n/a (no ffprobe)"; \
	  else g=$$(ffprobe -v error -skip_frame nokey -select_streams v:0 -show_entries frame=pts_time \
	         -of csv=p=0 -read_intervals %+20 "$$f" 2>/dev/null | tr -d , \
	         | awk 'NR>1 && $$1>p {print $$1-p} {p=$$1}' | sort -n | awk '{d[NR]=$$1} END{if(NR) printf "%.2f", d[int((NR+1)/2)]}'); \
	    if [ -z "$$g" ]; then echo "GOP unknown on $$(basename $$f) — keyframe mode off for such files"; \
	    else echo "GOP $${g}s on $$(basename $$f) — keyframe-only decode for cameras with interval >= $$(awk "BEGIN{printf \"%.1f\", $$g*0.9}")s"; fi; fi
	@printf "%-22s" "recordings:";       [ -d $(RECORDINGS) ] && \
	    echo "ok  ($(RECORDINGS), $$(find $(RECORDINGS) -maxdepth 1 -name '*.mp4' 2>/dev/null | wc -l) mp4 at top level)" \
	    || echo "MISSING — $(RECORDINGS) not present (nfs not mounted?)"
	@printf "%-22s" "recordings ro:";    ( touch $(RECORDINGS)/.wtest 2>/dev/null \
	    && rm -f $(RECORDINGS)/.wtest && echo "WRITABLE — fine locally, but on the detection box it should be ro" ) \
	    || echo "ok  (read-only)"
	@printf "%-22s" "detections dir:";   [ -d $(DETECTIONS) ] && \
	    ( [ -w $(DETECTIONS) ] && echo "ok  ($(DETECTIONS), writable)" || echo "NOT WRITABLE by $$(whoami)" ) \
	    || echo "MISSING — mkdir -p $(DETECTIONS)"
	@printf "%-22s" "manifests:";        echo "$$(find $(DETECTIONS) -name '*.json' 2>/dev/null | wc -l) written"
	@echo "=== end ==="

## status: how much has been processed, and what is left
status:
	@echo "recordings : $$(find $(RECORDINGS) -name '*.mp4' 2>/dev/null | wc -l)"
	@echo "manifests  : $$(find $(DETECTIONS) -name '*.json' 2>/dev/null | wc -l)"
	@echo "with events: $$(grep -rl '\"event_count\": [1-9]' $(DETECTIONS) --include='*.json' 2>/dev/null | wc -l)"
	@echo "thumbnails : $$(find $(DETECTIONS) -name '*.jpg' 2>/dev/null | wc -l)"
	@echo "failed     : $$(grep -rl '"error":' $(DETECTIONS) --include='*.json' 2>/dev/null | wc -l) written off, $$(find $(DETECTIONS) -name '*.failed' 2>/dev/null | wc -l) retrying"
	@echo "output size: $$(du -sh $(DETECTIONS) 2>/dev/null | cut -f1)"
	@[ -e $(JOBLOG) ] && echo "joblog     : $$(( $$(wc -l < $(JOBLOG)) - 1 )) jobs recorded" || echo "joblog     : none yet"
	@n=$$(pgrep -af 'detect_worker\.py|run_detection\.sh' 2>/dev/null \
	    | grep -cvE 'make|pgrep|grep' ); \
	  [ "$$n" -gt 0 ] && echo "running    : YES ($$n process(es))" || echo "running    : no"

# SINCE gives a relative window instead of an absolute --from, computed when
# the recipe runs (so it stays correct from cron). Accepts shorthand like
# 1h / 90m / 2d, or anything `date -d` understands ("yesterday", "2 hours ago").
# If both SINCE and FROM are given, SINCE wins.
SINCE_ARG = $(if $(SINCE),--from "$$(date -d "$$(echo '$(SINCE)' | sed -E \
    's/^([0-9]+)h$$/\1 hours ago/; s/^([0-9]+)m$$/\1 minutes ago/; s/^([0-9]+)d$$/\1 days ago/')" \
    '+%Y-%m-%d %H:%M')",)

## run: process recordings (CAMERA=, SINCE=1h, FROM=/TO=, LIMIT=N, FORCE=1)
run:
	$(RUNENV) $(NICE) $(RUNNER) \
	    $(if $(CAMERA),--camera '$(CAMERA)',) \
	    $(SINCE_ARG) \
	    $(if $(SINCE),,$(if $(FROM),--from '$(FROM)',)) \
	    $(if $(TO),--to '$(TO)',) \
	    $(if $(LIMIT),--limit $(LIMIT),) \
	    $(if $(FORCE),--force,)

## dry-run: show what 'make run' would process, without doing it
dry-run:
	$(RUNENV) $(RUNNER) \
	    $(if $(CAMERA),--camera '$(CAMERA)',) \
	    $(SINCE_ARG) \
	    $(if $(SINCE),,$(if $(FROM),--from '$(FROM)',)) \
	    $(if $(TO),--to '$(TO)',) \
	    $(if $(LIMIT),--limit $(LIMIT),) --dry-run

## index: rebuild the browse index from the manifests (ALL=1 to force)
index:
	$(PY) $(PREFIX)/build_index.py --detections $(DETECTIONS) $(if $(ALL),--all,)

## web: install the browse page next to the detections it reads
web: index
	sudo cp $(WEBPAGE) $(DETECTIONS)/
	@echo "installed $(DETECTIONS)/$(WEBPAGE)"
	@echo "add to nginx:  location /detections/ { alias $(DETECTIONS)/; index $(WEBPAGE); }"

## classes: list the object classes the current model can actually detect
classes:
	@$(PY) -c "from ultralytics import YOLO; import json,sys; \
	m=YOLO('$(if $(DETECT_MODEL),$(DETECT_MODEL),$(MODEL))'); \
	names=sorted(m.names.values()); \
	print('model knows %d classes:' % len(names)); \
	print('  ' + ', '.join(names)); \
	cfg=json.load(open('$(PREFIX)/$(CONFIG)')) if __import__('os').path.exists('$(PREFIX)/$(CONFIG)') else None; \
	want=set(cfg['default'].get('classes',[])) | {c for r in cfg.get('rules',[]) for c in r.get('classes',[])} if cfg else set(); \
	bad=sorted(want - set(names)); \
	print(); \
	print('NOT in this model (will never match): ' + ', '.join(bad)) if bad else print('every class in cameras.json exists in this model')"

## bench: summarize processing speed across manifests (per host and model)
bench:
	@$(PY) -c "$$BENCH_PY" "$(DETECTIONS)"

define BENCH_PY
import json,statistics,sys,collections,pathlib
root=pathlib.Path(sys.argv[1]); by=collections.defaultdict(list); old=0
for m in root.rglob('*.json'):
    if 'index' in m.parts: continue
    try: d=json.loads(m.read_text())
    except Exception: continue
    t=d.get('timing')
    if not t: old+=1; continue
    h=d.get('host',{})
    by[(h.get('name','?'), h.get('model','?'), d.get('worker',{}).get('version','-'), t.get('decoder','cv2'))].append(t)
if not by:
    print('no timed manifests yet' + (' (%d predate timing; reprocess with --force to measure)'%old if old else '')); raise SystemExit
def med(v): return round(statistics.median(v),1) if v else None
print('%-14s %-24s %-14s %-6s %5s %7s %9s %8s %7s %7s'%('host','model','version','decode','files','wall_s','xRealtime','infer_ms','warmup','decode%'))
print('-'*113)
for (host,model,ver,dec_name),ts in sorted(by.items()):
    rt=[t['realtime_factor'] for t in ts if t.get('realtime_factor')]
    wall=[t['wall_sec'] for t in ts]
    ips=[t['ms_per_inference'] for t in ts if t.get('ms_per_inference')]
    dec=[100*t['decode_sec']/t['wall_sec'] for t in ts if t.get('wall_sec')]
    wu=[t['warmup_sec'] for t in ts if t.get('warmup_sec')]
    print('%-14s %-24s %-14s %-6s %5d %7s %9s %8s %7s %6s%%'%(host[:14], pathlib.Path(model).name[:24], ver[:14], dec_name[:6],
          len(ts), med(wall), med(rt), med(ips) or '-', med(wu) or '-', med(dec)))
print()
allrt=[t['realtime_factor'] for ts in by.values() for t in ts if t.get('realtime_factor')]
if allrt:
    print('median %.1fx realtime -> one worker keeps up with ~%d continuous cameras'%(
          statistics.median(allrt), int(statistics.median(allrt))))
if old: print('%d manifest(s) predate timing; --force to re-measure'%old)
endef
export BENCH_PY

## prune: apply the retention policy now (THUMB_DAYS=, MANIFEST_DAYS=)
prune:
	DETECTIONS=$(DETECTIONS) RECORDINGS=$(RECORDINGS) \
	  THUMB_DAYS=$(THUMB_DAYS) MANIFEST_DAYS=$(MANIFEST_DAYS) \
	  $(PREFIX)/prune_detections.sh

## install-cron: every 10 min under flock, nightly prune, plus log rotation
install-cron: install-logrotate
	@echo "==> writing /etc/cron.d/detection"
	@printf '%s\n' \
	  'SHELL=/bin/bash' \
	  'PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin' \
	  '*/10 * * * * $(shell whoami) flock -n /tmp/detect.lock env $(RUNENV) $(NICE) $(RUNNER) --limit $(CRON_LIMIT) >> $(LOGFILE) 2>&1 && $(PY) $(PREFIX)/build_index.py --detections $(DETECTIONS) >> $(LOGFILE) 2>&1' \
	  '$(PRUNE_AT) * * * $(shell whoami) DETECTIONS=$(DETECTIONS) RECORDINGS=$(RECORDINGS) THUMB_DAYS=$(THUMB_DAYS) MANIFEST_DAYS=$(MANIFEST_DAYS) $(PREFIX)/prune_detections.sh >> $(LOGFILE) 2>&1' \
	  | sudo tee /etc/cron.d/detection >/dev/null
	sudo chmod 644 /etc/cron.d/detection
	@sudo touch $(LOGFILE) && sudo chown $(shell whoami) $(LOGFILE)
	@echo "installed. watch it with: tail -f $(LOGFILE)"

## install-logrotate: keep /var/log/detection.log bounded
install-logrotate:
	@echo "==> writing /etc/logrotate.d/detection"
	@printf '%s\n' \
	  '$(LOGFILE) {' \
	  '    daily' \
	  '    rotate 7' \
	  '    compress' \
	  '    delaycompress' \
	  '    missingok' \
	  '    notifempty' \
	  '    # copytruncate, not create: cron holds the log open through >>' \
	  '    # for the whole run, so a rename would send writes to the' \
	  '    # rotated file and lose any run in progress at rotation time.' \
	  '    copytruncate' \
	  '}' \
	  | sudo tee /etc/logrotate.d/detection >/dev/null
	sudo chmod 644 /etc/logrotate.d/detection
	@echo "    check it with: logrotate -d /etc/logrotate.d/detection"

## uninstall-cron: remove the cron entries and log rotation
uninstall-cron:
	sudo rm -f /etc/cron.d/detection /etc/logrotate.d/detection
	@echo "cron entries and logrotate rule removed."

## test: end-to-end check on one real recording (FILE=... to choose)
test:
	@f="$(FILE)"; \
	[ -n "$$f" ] || f=$$(find $(RECORDINGS) -name '*.mp4' -mmin +2 2>/dev/null | head -1); \
	[ -n "$$f" ] || { echo "no recording found under $(RECORDINGS)"; exit 1; }; \
	echo "==> testing on $$f"; \
	rm -rf /tmp/detect-test && mkdir -p /tmp/detect-test; \
	$(if $(DETECT_MODEL),DETECT_MODEL=$(DETECT_MODEL),) \
	$(if $(DECODE),DECODE=$(DECODE),) \
	$(if $(INTERVAL_SCALE),INTERVAL_SCALE=$(INTERVAL_SCALE),) \
	$(if $(DECODE_KEYFRAMES),DECODE_KEYFRAMES=$(DECODE_KEYFRAMES),) \
	CAMERA_RULES=$(PREFIX)/$(CONFIG) \
	$(PY) $(PREFIX)/detect_worker.py --input "$$f" \
	    --input-root $(RECORDINGS) --output-root /tmp/detect-test; \
	echo; echo "==> manifest:"; \
	find /tmp/detect-test -name '*.json' -exec $(PY) -m json.tool {} \; | head -40

## uninstall: remove scripts and venv, KEEP detections and config
uninstall: uninstall-cron
	sudo rm -rf $(VENV)
	cd $(PREFIX) 2>/dev/null && sudo rm -f $(SCRIPTS) || true
	@echo "removed venv and scripts from $(PREFIX)."
	@echo "kept: $(PREFIX)/$(CONFIG) and everything in $(DETECTIONS)"
	@echo "to remove those too: make purge"

## purge: uninstall AND delete all detections output (destructive)
purge: uninstall
	@read -p "delete ALL detections in $(DETECTIONS)? [y/N] " a; \
	  [ "$$a" = y ] || { echo "aborted."; exit 1; }; \
	  sudo rm -rf $(DETECTIONS) $(PREFIX); \
	  echo "purged $(DETECTIONS) and $(PREFIX)."

## clean: remove local build junk (not the install)
clean:
	rm -rf __pycache__ *.pyc /tmp/detect-test
