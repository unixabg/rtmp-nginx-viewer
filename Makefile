.PHONY: install upgrade uninstall purge

WEB_ROOT=/var/www/html
NGINX_CONF_DIR=/etc/nginx
SITES_AVAILABLE=$(NGINX_CONF_DIR)/sites-available
SITES_ENABLED=$(NGINX_CONF_DIR)/sites-enabled

install:
	# Backup existing configurations and web content if they exist
	[ -f $(NGINX_CONF_DIR)/nginx.conf ] && cp $(NGINX_CONF_DIR)/nginx.conf $(NGINX_CONF_DIR)/nginx.conf.backup || true
	[ -f $(SITES_AVAILABLE)/default ] && cp $(SITES_AVAILABLE)/default $(SITES_AVAILABLE)/default.backup || true
	[ -f $(NGINX_CONF_DIR)/cameras.conf ] && cp $(NGINX_CONF_DIR)/cameras.conf $(NGINX_CONF_DIR)/cameras.conf.backup || true
	[ -f $(WEB_ROOT)/index.html ] && cp $(WEB_ROOT)/index.html $(WEB_ROOT)/index.html.backup || true

	# Copy new configurations and web content
	install -m 644 nginx.conf $(NGINX_CONF_DIR)/
	install -m 644 default $(SITES_AVAILABLE)/
	ln -sfn $(SITES_AVAILABLE)/default $(SITES_ENABLED)/default
	install -m 644 cameras.conf $(NGINX_CONF_DIR)/
	install -m 644 history.html $(WEB_ROOT)/
	install -m 644 index.html $(WEB_ROOT)/
	install -m 644 kiosk.html $(WEB_ROOT)/
	install -m 644 live.html $(WEB_ROOT)/
	install -m 644 style.css $(WEB_ROOT)/
	install -m 644 version.txt $(WEB_ROOT)/

	# Reload Nginx if installed and active
	if systemctl is-active --quiet nginx; then \
		systemctl reload nginx; \
	elif command -v nginx > /dev/null; then \
		nginx -s reload; \
	fi

upgrade:
	@echo "Upgrading components: index.html, history.html, and version.txt to /var/www/html/"
	install -m 644 history.html $(WEB_ROOT)/
	install -m 644 index.html $(WEB_ROOT)/
	install -m 644 kiosk.html $(WEB_ROOT)/
	install -m 644 live.html $(WEB_ROOT)/
	install -m 644 style.css $(WEB_ROOT)/
	install -m 644 version.txt $(WEB_ROOT)/
	@echo "Upgrade complete."

uninstall:
	# Restore backup configurations if they exist
	[ -f $(NGINX_CONF_DIR)/nginx.conf.backup ] && mv $(NGINX_CONF_DIR)/nginx.conf.backup $(NGINX_CONF_DIR)/nginx.conf || true
	[ -f $(SITES_AVAILABLE)/default.backup ] && mv $(SITES_AVAILABLE)/default.backup $(SITES_AVAILABLE)/default || true
	[ -f $(NGINX_CONF_DIR)/cameras.conf.backup ] && mv $(NGINX_CONF_DIR)/cameras.conf.backup $(NGINX_CONF_DIR)/cameras.conf || true

	# Restore original web content if backup exists
	[ -f $(WEB_ROOT)/index.html.backup ] && mv $(WEB_ROOT)/index.html.backup $(WEB_ROOT)/index.html || true

	# Remove style.css and history.html that were added by install
	rm -f $(WEB_ROOT)/style.css
	rm -f $(WEB_ROOT)/history.html
	rm -f $(WEB_ROOT)/kiosk.html
	rm -f $(WEB_ROOT)/live.html

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

