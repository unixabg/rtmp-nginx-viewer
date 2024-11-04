.PHONY: install uninstall purge

WEB_ROOT=/var/www/html
NGINX_CONF_DIR=/etc/nginx
SITES_AVAILABLE=$(NGINX_CONF_DIR)/sites-available
SITES_ENABLED=$(NGINX_CONF_DIR)/sites-enabled

install:
	# Backup existing configurations and web content
	cp $(NGINX_CONF_DIR)/nginx.conf $(NGINX_CONF_DIR)/nginx.conf.backup
	cp $(SITES_AVAILABLE)/default $(SITES_AVAILABLE)/default.backup
	[ -f $(NGINX_CONF_DIR)/cameras.conf ] && cp $(NGINX_CONF_DIR)/cameras.conf $(NGINX_CONF_DIR)/cameras.conf.backup
	[ -f $(WEB_ROOT)/index.html ] && cp $(WEB_ROOT)/index.html $(WEB_ROOT)/index.html.backup

	# Copy new configurations and web content
	cp nginx.conf $(NGINX_CONF_DIR)/nginx.conf
	cp default $(SITES_AVAILABLE)/default
	ln -sfn $(SITES_AVAILABLE)/default $(SITES_ENABLED)/default
	cp cameras.conf $(NGINX_CONF_DIR)/
	cp index.html $(WEB_ROOT)/index.html
	cp index.html $(WEB_ROOT)/history.html
	cp style.css $(WEB_ROOT)/style.css

	# Reload Nginx to apply changes
	systemctl reload nginx

uninstall:
	# Restore backup configurations
	mv $(NGINX_CONF_DIR)/nginx.conf.backup $(NGINX_CONF_DIR)/nginx.conf
	mv $(SITES_AVAILABLE)/default.backup $(SITES_AVAILABLE)/default
	# Restore original cameras.conf if backup exists
	[ -f $(NGINX_CONF_DIR)/cameras.conf.backup ] && mv $(NGINX_CONF_DIR)/cameras.conf.backup $(NGINX_CONF_DIR)/cameras.conf

	# Restore original web content if backup exists
	[ -f $(WEB_ROOT)/index.html.backup ] && mv $(WEB_ROOT)/index.html.backup $(WEB_ROOT)/index.html

	# Remove style.css and history.html that was added by the install
	rm $(WEB_ROOT)/style.css
	rm $(WEB_ROOT)/history.html

	# Reload Nginx to apply changes
	systemctl reload nginx

purge:
	# Call uninstall target
	$(MAKE) uninstall

	# Remove additional files created by install, if any
	rm $(NGINX_CONF_DIR)/cameras.conf

