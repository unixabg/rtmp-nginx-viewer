.PHONY: install uninstall purge upgrade

install:
	# Ensure necessary directories exist with appropriate permissions
	install -d -m 755 /etc/rtsp-to-rtmp
	install -d -m 755 /opt
	install -d -m 755 /var/log/rtsp-to-rtmp

	# Install the main script
	install -m 755 rtsp-to-rtmp.sh /opt/rtsp-to-rtmp

	# Install logrotate configuration
	install -m 644 rtsp-to-rtmp.logrotate /etc/logrotate.d/rtsp-to-rtmp

	# Install cameras configuration file
	install -m 644 rtsp-to-rtmp.cameras /etc/rtsp-to-rtmp/cameras

	# Install the systemd service file
	install -m 644 rtsp-to-rtmp.service /etc/systemd/system/

	# Reload systemd to apply changes
	systemctl daemon-reload

uninstall:
# Stop the systemd service
	systemctl stop rtsp-to-rtmp.service

	# Disable the systemd service to not start on boot
	systemctl disable rtsp-to-rtmp.service

	# Remove the systemd service file
	rm -f /etc/systemd/system/rtsp-to-rtmp.service

	# Reload systemd to apply changes
	systemctl daemon-reload

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

upgrade:
	# Install the latest version of the script while preserving permissions
	install -m 755 rtsp-to-rtmp.sh /opt/rtsp-to-rtmp

	# Restart the service to use the upgraded script
	systemctl restart rtsp-to-rtmp.service
