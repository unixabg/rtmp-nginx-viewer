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
	systemctl stop rtsp-to-rtmp.service || true

	# Disable the systemd service to not start on boot
	systemctl disable rtsp-to-rtmp.service || true

	# Remove the systemd service file
	rm -f /etc/systemd/system/rtsp-to-rtmp.service

	# Reload systemd to apply changes
	systemctl daemon-reload

	# Remove the main script
	rm -f /opt/rtsp-to-rtmp

	# Remove the logrotate configuration
	rm -f /etc/logrotate.d/rtsp-to-rtmp

	# Notify the user about logs
	@echo "Log files in /var/log/rtsp-to-rtmp/ are not removed. Please delete them manually if needed."
	# Notify the user about configs
	@echo "Configuration files in /etc/rtsp-to-rtmp/ are not removed. Please delete them manually if needed."

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

	# Install the latest logrotate configuration
	install -m 644 rtsp-to-rtmp.logrotate /etc/logrotate.d/rtsp-to-rtmp

	# Reload systemd to apply changes
	systemctl daemon-reload

	# Restart the service to use the upgraded script
	systemctl restart rtsp-to-rtmp.service

