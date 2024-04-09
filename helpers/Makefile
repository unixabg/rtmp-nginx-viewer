.PHONY: install

install:
	# Ensure /opt and /etc/rtsp-to-rtmp directories exist
	mkdir -p /etc/rtsp-to-rtmp
	mkdir -p /opt
	mkdir -p /var/log/rtsp-to-rtmp

	# Copy rtsp-to-rtmp.sh to /opt
	cp rtsp-to-rtmp.sh /opt/rtsp-to-rtmp
	chmod +x /opt/rtsp-to-rtmp

	# Install logrotate configuration
	cp rtsp-to-rtmp.logrotate /etc/logrotate.d/rtsp-to-rtmp

	# Copy cameras.conf to /etc/rtsp-to-rtmp
	cp rtsp-to-rtmp.cameras /etc/rtsp-to-rtmp/cameras

uninstall:
	# Remove the main script
	rm -f /opt/rtsp-to-rtmp

	# Optionally remove the logrotate configuration
	# Comment out the next line if you prefer to keep the logrotate config on uninstall
	rm -f /etc/logrotate.d/rtsp-to-rtmp

	# Note: This target does not remove the configuration directory or log files

purge:
	# Call uninstall target
	$(MAKE) uninstall

	# Remove configuration files and directory
	rm -rf /etc/rtsp-to-rtmp

	# Remove all related log files
	rm -rf /var/log/rtsp-to-rtmp
