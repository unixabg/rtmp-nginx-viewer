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
GPU         ?= 0
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
               JOBLOG=$(JOBLOG) JOBS=$(JOBS) $(if $(DETECT_MODEL),DETECT_MODEL=$(DETECT_MODEL),)

SCRIPTS     := detect_worker.py run_detection.sh prune_detections.sh build_index.py
WEBPAGE     := detections.html
CONFIG      := cameras.json

.PHONY: help install install-deps install-scripts install-config model \
        upgrade upgrade-scripts upgrade-deps upgrade-web uninstall purge \
        doctor status run dry-run prune index web classes bench \
        install-cron uninstall-cron test clean

## help: show this list
help:
	@echo "detection add-on — targets:"; echo
	@grep -E '^## ' $(MAKEFILE_LIST) | sed 's/^## //' | \
	    awk -F': ' '{printf "  %-18s %s\n", $$1, $$2}'
	@echo
	@echo "current settings (override on the command line):"
	@printf "  %-12s %s\n" PREFIX $(PREFIX) RECORDINGS $(RECORDINGS) \
	    DETECTIONS $(DETECTIONS) JOBS $(JOBS) GPU $(GPU) ACCEL $(ACCEL)
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
	sudo apt-get install -y python3-venv parallel
	@echo "==> venv at $(VENV)"
	sudo mkdir -p $(PREFIX) $(DETECTIONS) $(TMPDIR_PIP)
	[ -x $(PY) ] || sudo python3 -m venv $(VENV)
ifeq ($(GPU),1)
	@echo "==> torch with CUDA (GPU=1)"
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
	@echo "==> scripts -> $(PREFIX)"
	sudo mkdir -p $(PREFIX)
	sudo cp $(SCRIPTS) $(PREFIX)/
	sudo chmod +x $(PREFIX)/*.sh

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
	@echo "==> refreshing scripts in $(PREFIX)"
	sudo cp $(SCRIPTS) $(PREFIX)/
	sudo chmod +x $(PREFIX)/*.sh
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
	@printf "%-22s" "cameras.json:"; \
	  p=$$([ -x $(PY) ] && echo $(PY) || command -v python3); \
	  if [ ! -e $(PREFIX)/$(CONFIG) ]; then echo "absent (defaults will be used)"; \
	  elif [ -z "$$p" ]; then echo "present (no python available to validate)"; \
	  elif $$p -c "import json;json.load(open('$(PREFIX)/$(CONFIG)'))" 2>/dev/null; then \
	    echo "ok  (valid json, $$($$p -c "import json;print(len(json.load(open('$(PREFIX)/$(CONFIG)')).get('rules',[])))") rule(s))"; \
	  else echo "INVALID JSON — worker will fall back to defaults"; fi
	@printf "%-22s" "torch:";            $(PY) -c "import torch;print('ok  ',torch.__version__,'cuda',torch.cuda.is_available())" 2>/dev/null || echo "MISSING"
	@printf "%-22s" "opencv:";           $(PY) -c "import cv2;print('ok  ',cv2.__version__)" 2>/dev/null || echo "MISSING"
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
    by[(h.get('name','?'), h.get('model','?'))].append(t)
if not by:
    print('no timed manifests yet' + (' (%d predate timing; reprocess with --force to measure)'%old if old else '')); raise SystemExit
def med(v): return round(statistics.median(v),1) if v else None
print('%-16s %-30s %6s %8s %9s %9s %8s %7s'%('host','model','files','wall_s','xRealtime','infer_ms','warmup_s','decode%'))
print('-'*104)
for (host,model),ts in sorted(by.items()):
    rt=[t['realtime_factor'] for t in ts if t.get('realtime_factor')]
    wall=[t['wall_sec'] for t in ts]
    ips=[t['ms_per_inference'] for t in ts if t.get('ms_per_inference')]
    dec=[100*t['decode_sec']/t['wall_sec'] for t in ts if t.get('wall_sec')]
    wu=[t['warmup_sec'] for t in ts if t.get('warmup_sec')]
    print('%-16s %-30s %6d %8s %9s %9s %8s %6s%%'%(host[:16], pathlib.Path(model).name[:30],
          len(ts), med(wall), med(rt), med(ips) or '-', med(wu) or '-', med(dec)))
print()
allrt=[t['realtime_factor'] for ts in by.values() for t in ts if t.get('realtime_factor')]
if allrt:
    print('median %.1fx realtime -> one worker keeps up with ~%d continuous cameras'%(
          statistics.median(allrt), int(statistics.median(allrt))))
if old: print('%d manifest(s) predate timing; --force to re-measure'%old)
endef
export BENCH_PY

## prune: apply the retention policy to the detections tree
prune:
	DETECTIONS=$(DETECTIONS) RECORDINGS=$(RECORDINGS) $(PREFIX)/prune_detections.sh

## install-cron: every 10 min under flock, plus a nightly prune
install-cron:
	@echo "==> writing /etc/cron.d/detection"
	@printf '%s\n' \
	  'SHELL=/bin/bash' \
	  'PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin' \
	  '*/10 * * * * $(shell whoami) flock -n /tmp/detect.lock env $(RUNENV) $(NICE) $(RUNNER) --limit $(CRON_LIMIT) >> /var/log/detection.log 2>&1 && $(PY) $(PREFIX)/build_index.py --detections $(DETECTIONS) >> /var/log/detection.log 2>&1' \
	  '30 2 * * * $(shell whoami) DETECTIONS=$(DETECTIONS) RECORDINGS=$(RECORDINGS) $(PREFIX)/prune_detections.sh >> /var/log/detection.log 2>&1' \
	  | sudo tee /etc/cron.d/detection >/dev/null
	sudo chmod 644 /etc/cron.d/detection
	@sudo touch /var/log/detection.log && sudo chown $(shell whoami) /var/log/detection.log
	@echo "installed. watch it with: tail -f /var/log/detection.log"

## uninstall-cron: remove the cron entries
uninstall-cron:
	sudo rm -f /etc/cron.d/detection
	@echo "cron entries removed."

## test: end-to-end check on one real recording (FILE=... to choose)
test:
	@f="$(FILE)"; \
	[ -n "$$f" ] || f=$$(find $(RECORDINGS) -name '*.mp4' -mmin +2 2>/dev/null | head -1); \
	[ -n "$$f" ] || { echo "no recording found under $(RECORDINGS)"; exit 1; }; \
	echo "==> testing on $$f"; \
	rm -rf /tmp/detect-test && mkdir -p /tmp/detect-test; \
	$(if $(DETECT_MODEL),DETECT_MODEL=$(DETECT_MODEL),) \
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
