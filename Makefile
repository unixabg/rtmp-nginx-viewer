.PHONY: install upgrade uninstall purge

WEB_ROOT=/var/www/html
NGINX_CONF_DIR=/etc/nginx
SITES_AVAILABLE=$(NGINX_CONF_DIR)/sites-available
SITES_ENABLED=$(NGINX_CONF_DIR)/sites-enabled

# ----- Minimal pinned versions for local JS/CSS -----
HLSJS_VER      ?= 1.5.8
OVENPLAYER_VER ?= 0.10.19
FLATPICKR_VER  ?= 4.6.13

# Dependency check target
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

install: check_deps
	# Backup existing configurations and web content if they exist
	[ -f $(NGINX_CONF_DIR)/nginx.conf ] && cp $(NGINX_CONF_DIR)/nginx.conf $(NGINX_CONF_DIR)/nginx.conf.backup || true
	[ -f $(SITES_AVAILABLE)/default ] && cp $(SITES_AVAILABLE)/default $(SITES_AVAILABLE)/default.backup || true
	[ -f $(NGINX_CONF_DIR)/cameras.conf ] && cp $(NGINX_CONF_DIR)/cameras.conf $(NGINX_CONF_DIR)/cameras.conf.backup || true
	[ -f $(WEB_ROOT)/index.html ] && cp $(WEB_ROOT)/index.html $(WEB_ROOT)/index.html.backup || true

	# Install scripts
	install -m 755 nginx-thumbs.sh /opt/nginx-thumbs

	# Copy new configurations and web content
	install -m 644 nginx.conf $(NGINX_CONF_DIR)/
	install -m 644 default $(SITES_AVAILABLE)/
	ln -sfn $(SITES_AVAILABLE)/default $(SITES_ENABLED)/default
	install -m 644 cameras.conf $(NGINX_CONF_DIR)/
	install -m 644 camera-status.html $(WEB_ROOT)/
	install -m 644 history.html $(WEB_ROOT)/
	install -m 644 index.html $(WEB_ROOT)/
	install -m 644 kiosk.html $(WEB_ROOT)/
	install -m 644 live.html $(WEB_ROOT)/
	install -m 644 style.css $(WEB_ROOT)/
	install -m 644 version.txt $(WEB_ROOT)/

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

	# Remove vendored assets
	rm -rf $(WEB_ROOT)/vendor

	# Reload Nginx if installed and active
	if systemctl is-active --quiet nginx; then \
		systemctl restart nginx; \
	elif command -v nginx > /dev/null; then \
		nginx -s reload; \
	fi

purge: uninstall
	# Call purge in helpers/Makefile
	$(MAKE) -C helpers purge

	# Remove additional files created by install, if any
	rm -f $(NGINX_CONF_DIR)/cameras.conf
	# Remove scripts
	rm -f /opt/nginx-thumbs

