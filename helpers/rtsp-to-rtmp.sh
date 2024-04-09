#!/bin/bash

CONFIG_FILE="/etc/rtsp-to-rtmp/cameras"
RTMP_SERVER_URL="rtmp://localhost/cam"
declare -a CHILD_PIDS
declare -a FIFO_PATHS

# Function to handle script termination gracefully
cleanup() {
	echo "Shutting down script gracefully..."
	# Kill all child processes associated with FFmpeg and FIFO handling
	for PID in "${CHILD_PIDS[@]}"; do
		kill -SIGTERM "$PID" 2>/dev/null
	done
	# Remove FIFOs
	for FIFO in "${FIFO_PATHS[@]}"; do
		[[ -p "$FIFO" ]] && rm -f "$FIFO"
	done
	exit 0
}
trap cleanup SIGINT SIGTERM

while IFS=' ' read -r CAMERA_NAME RTSP_URL; do
	[[ -z "$CAMERA_NAME" || -z "$RTSP_URL" || "$CAMERA_NAME" =~ ^# ]] && continue

	FIFO_PATH="/tmp/ffmpeg_log_${CAMERA_NAME}.fifo"
	LOG_FILE="/var/log/rtsp-to-rtmp/${CAMERA_NAME}.log"
	FIFO_PATHS+=("$FIFO_PATH")

	# Create FIFO if it does not exist
	[[ ! -p "$FIFO_PATH" ]] && mkfifo "$FIFO_PATH"

	# Start a background process to redirect FIFO output to the log file
	cat "$FIFO_PATH" >> "$LOG_FILE" &
	CHILD_PIDS+=("$!")

	# Launch FFmpeg using the FIFO for logging
	(
		RETRY_DELAY=5
		MAX_RETRY_DELAY=60
		while true; do
			ffmpeg -timeout 5000000 -i "$RTSP_URL" -acodec copy -vcodec copy -f flv "${RTMP_SERVER_URL}/${CAMERA_NAME}" > "$FIFO_PATH" 2>&1
			echo "$(date) - Stream $CAMERA_NAME died, restarting in $RETRY_DELAY seconds..."
			sleep $RETRY_DELAY
			[ $RETRY_DELAY -lt $MAX_RETRY_DELAY ] && RETRY_DELAY=$((RETRY_DELAY * 2))
			[ $RETRY_DELAY -gt $MAX_RETRY_DELAY ] && RETRY_DELAY=$MAX_RETRY_DELAY
		done
	) &
	CHILD_PIDS+=("$!")
done < "$CONFIG_FILE"

wait

