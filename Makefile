.PHONY: help install upgrade uninstall purge check_deps

# `make` on its own prints the help rather than running the first target,
# which would otherwise be check_deps and look like nothing happened.
.DEFAULT_GOAL := help

WEB_ROOT=/var/www/html
NGINX_CONF_DIR=/etc/nginx
SITES_AVAILABLE=$(NGINX_CONF_DIR)/sites-available
SITES_ENABLED=$(NGINX_CONF_DIR)/sites-enabled

# ----- Minimal pinned versions for local JS/CSS -----
HLSJS_VER      ?= 1.5.8
OVENPLAYER_VER ?= 0.10.43
FLATPICKR_VER  ?= 4.6.13

## help: this list
help:
	@echo "rtmp-nginx-viewer $$(cut -d' ' -f2 version.txt) — targets:"; echo
	@grep -E '^## ' $(MAKEFILE_LIST) | sed 's/^## //' | \
	    awk -F': ' '{printf "  %-14s %s\n", $$1, $$2}'
	@echo
	@echo "paths (override on the command line):"
	@printf "  %-16s %s\n" WEB_ROOT $(WEB_ROOT) NGINX_CONF_DIR $(NGINX_CONF_DIR)
	@echo
	@echo "  upgrade refreshes web content only and never touches your"
	@echo "  nginx configuration or sitename.txt — see README.md."

# Dependency check target
## check_deps: report any missing apt packages
REQUIRED_PKGS := build-essential ffmpeg vim git nginx libnginx-mod-rtmp curl

check_deps:
	@echo "Checking required packages..."
	@missing=""; \
	for pkg in $(REQUIRED_PKGS); do \
		if ! dpkg -s $$pkg >/dev/null 2>&1; then \
			echo "Missing: $$pkg"; \
			missing="$$missing $$pkg"; \
		else \
			echo "Found: $$pkg"; \
		fi; \
	done; \
	if [ -n "$$missing" ]; then \
		echo ""; \
		echo "The following packages are missing:$$missing"; \
		echo "You can install them with:"; \
		echo "  sudo apt install$$missing"; \
		exit 1; \
	else \
		echo "All dependencies are installed."; \
	fi

## install: first-time install (backs up existing nginx config and index.html)
install: check_deps
	# Backup existing configurations and web content if they exist
	[ -f $(NGINX_CONF_DIR)/nginx.conf ] && cp $(NGINX_CONF_DIR)/nginx.conf $(NGINX_CONF_DIR)/nginx.conf.backup || true
	[ -f $(SITES_AVAILABLE)/default ] && cp $(SITES_AVAILABLE)/default $(SITES_AVAILABLE)/default.backup || true
	[ -f $(NGINX_CONF_DIR)/cameras.conf ] && cp $(NGINX_CONF_DIR)/cameras.conf $(NGINX_CONF_DIR)/cameras.conf.backup || true
	[ -f $(NGINX_CONF_DIR)/cameras-sub.conf ] && cp $(NGINX_CONF_DIR)/cameras-sub.conf $(NGINX_CONF_DIR)/cameras-sub.conf.backup || true
	[ -f $(WEB_ROOT)/index.html ] && cp $(WEB_ROOT)/index.html $(WEB_ROOT)/index.html.backup || true

	# Media directories. Created whether or not recording is enabled, so
	# the nginx location blocks always resolve; index.html probes for
	# CONTENT rather than existence and hides the nav entries for
	# whichever of these is still empty.
	install -d -m 755 /videos/recordings /videos/thumbnails /videos/detections

	# Install scripts
	install -m 755 nginx-thumbs.sh /opt/nginx-thumbs

	# Copy new configurations and web content
	install -m 644 nginx.conf $(NGINX_CONF_DIR)/
	install -m 644 default $(SITES_AVAILABLE)/
	ln -sfn $(SITES_AVAILABLE)/default $(SITES_ENABLED)/default
	install -m 644 cameras.conf $(NGINX_CONF_DIR)/
	install -m 644 cameras-sub.conf $(NGINX_CONF_DIR)/
	install -m 644 camera-status.html $(WEB_ROOT)/
	install -m 644 history.html $(WEB_ROOT)/
	install -m 644 index.html $(WEB_ROOT)/
	install -m 644 kiosk.html $(WEB_ROOT)/
	install -m 644 live.html $(WEB_ROOT)/
	install -m 644 style.css $(WEB_ROOT)/
	install -m 644 version.txt $(WEB_ROOT)/
	# Site name shown on the home page. Created once and never
	# overwritten, so `make upgrade` cannot clobber a local name.
	[ -f $(WEB_ROOT)/sitename.txt ] || install -m 644 sitename.txt $(WEB_ROOT)/
	# Install cron job for recording housekeeping
	install -m 644 crontab /etc/cron.d/rtmp-nginx-viewer

	# Minimal: fetch JS/CSS deps directly into WEB_ROOT
	install -d $(WEB_ROOT)/vendor/hls $(WEB_ROOT)/vendor/ovenplayer $(WEB_ROOT)/vendor/flatpickr
	curl -fsSL "https://cdn.jsdelivr.net/npm/hls.js@$(HLSJS_VER)/dist/hls.min.js" \
		-o "$(WEB_ROOT)/vendor/hls/hls.min.js"
	curl -fsSL "https://cdn.jsdelivr.net/npm/ovenplayer@$(OVENPLAYER_VER)/dist/ovenplayer.js" \
		-o "$(WEB_ROOT)/vendor/ovenplayer/ovenplayer.js"
	# Flatpickr: include unminified (to mirror <script src=\"https://cdn.jsdelivr.net/npm/flatpickr\">) and minified + CSS
	curl -fsSL "https://cdn.jsdelivr.net/npm/flatpickr@$(FLATPICKR_VER)/dist/flatpickr.js" \
		-o "$(WEB_ROOT)/vendor/flatpickr/flatpickr.js"
	curl -fsSL "https://cdn.jsdelivr.net/npm/flatpickr@$(FLATPICKR_VER)/dist/flatpickr.min.js" \
		-o "$(WEB_ROOT)/vendor/flatpickr/flatpickr.min.js"
	curl -fsSL "https://cdn.jsdelivr.net/npm/flatpickr@$(FLATPICKR_VER)/dist/flatpickr.min.css" \
		-o "$(WEB_ROOT)/vendor/flatpickr/flatpickr.min.css"

	# Reload Nginx if installed and active
	if systemctl is-active --quiet nginx; then \
		systemctl reload nginx; \
	elif command -v nginx > /dev/null; then \
		nginx -s reload; \
	fi

## upgrade: refresh web pages and vendored assets only
upgrade:
	# Install scripts
	install -m 755 nginx-thumbs.sh /opt/nginx-thumbs
	@echo "Upgrading components to /var/www/html and scripts in /opt"
	install -m 644 style.css $(WEB_ROOT)/
	install -m 644 camera-status.html $(WEB_ROOT)/
	install -m 644 history.html $(WEB_ROOT)/
	install -m 644 index.html $(WEB_ROOT)/
	install -m 644 kiosk.html $(WEB_ROOT)/
	install -m 644 live.html $(WEB_ROOT)/
	install -m 644 version.txt $(WEB_ROOT)/
	# Only if absent - see install. An upgrade never rewrites the name.
	[ -f $(WEB_ROOT)/sitename.txt ] || install -m 644 sitename.txt $(WEB_ROOT)/

	# Minimal: refresh JS/CSS deps directly into WEB_ROOT
	install -d $(WEB_ROOT)/vendor/hls $(WEB_ROOT)/vendor/ovenplayer $(WEB_ROOT)/vendor/flatpickr
	curl -fsSL "https://cdn.jsdelivr.net/npm/hls.js@$(HLSJS_VER)/dist/hls.min.js" \
		-o "$(WEB_ROOT)/vendor/hls/hls.min.js"
	curl -fsSL "https://cdn.jsdelivr.net/npm/ovenplayer@$(OVENPLAYER_VER)/dist/ovenplayer.js" \
		-o "$(WEB_ROOT)/vendor/ovenplayer/ovenplayer.js"
	# Flatpickr: include unminified and minified + CSS
	curl -fsSL "https://cdn.jsdelivr.net/npm/flatpickr@$(FLATPICKR_VER)/dist/flatpickr.js" \
		-o "$(WEB_ROOT)/vendor/flatpickr/flatpickr.js"
	curl -fsSL "https://cdn.jsdelivr.net/npm/flatpickr@$(FLATPICKR_VER)/dist/flatpickr.min.js" \
		-o "$(WEB_ROOT)/vendor/flatpickr/flatpickr.min.js"
	curl -fsSL "https://cdn.jsdelivr.net/npm/flatpickr@$(FLATPICKR_VER)/dist/flatpickr.min.css" \
		-o "$(WEB_ROOT)/vendor/flatpickr/flatpickr.min.css"

	@echo "Upgrade complete."

## uninstall: remove web content and restore the backed-up config
uninstall:
	# Restore backup configurations if they exist
	[ -f $(NGINX_CONF_DIR)/nginx.conf.backup ] && mv $(NGINX_CONF_DIR)/nginx.conf.backup $(NGINX_CONF_DIR)/nginx.conf || true
	[ -f $(SITES_AVAILABLE)/default.backup ] && mv $(SITES_AVAILABLE)/default.backup $(SITES_AVAILABLE)/default || true
	[ -f $(NGINX_CONF_DIR)/cameras.conf.backup ] && mv $(NGINX_CONF_DIR)/cameras.conf.backup $(NGINX_CONF_DIR)/cameras.conf || true

	# Restore original web content if backup exists
	[ -f $(WEB_ROOT)/index.html.backup ] && mv $(WEB_ROOT)/index.html.backup $(WEB_ROOT)/index.html || true

	# Remove html items
	rm -f $(WEB_ROOT)/style.css
	rm -f $(WEB_ROOT)/camera-status.html
	rm -f $(WEB_ROOT)/history.html
	rm -f $(WEB_ROOT)/index.html
	rm -f $(WEB_ROOT)/kiosk.html
	rm -f $(WEB_ROOT)/live.html
	rm -f $(WEB_ROOT)/version.txt
	# Remove cron job
	rm -f /etc/cron.d/rtmp-nginx-viewer

	# Remove vendored assets
	rm -rf $(WEB_ROOT)/vendor

	# Reload Nginx if installed and active
	if systemctl is-active --quiet nginx; then \
		systemctl restart nginx; \
	elif command -v nginx > /dev/null; then \
		nginx -s reload; \
	fi

## purge: uninstall, plus cameras.conf, scripts and sitename.txt
purge: uninstall
	# Helpers are installed and removed from their own directories, so a
	# top-level purge deliberately leaves them alone - `make purge` in
	# helpers/detector, helpers/nvr-backup, etc. There is no
	# helpers/Makefile; this line never worked and is kept only as a
	# marker in case a fan-out target is added later.
	#$(MAKE) -C helpers purge

	# Remove additional files created by install, if any
	rm -f $(NGINX_CONF_DIR)/cameras.conf
	# Local site name: kept by uninstall, removed by purge
	rm -f $(WEB_ROOT)/sitename.txt
	# Remove scripts
	rm -f /opt/nginx-thumbs

