#!/bin/bash

# Write our pid file for logrotate signal
echo $$ > /var/run/rtsp-to-rtmp.pid

CONFIG_FILE="/etc/rtsp-to-rtmp/cameras"
RTMP_SERVER_URL="rtmp://localhost/cam"
declare -a CHILD_PIDS
declare -a FIFO_PATHS
SCRIPT_LOG="/var/log/rtsp-to-rtmp/script_log"

# Function to handle script termination gracefully
cleanup() {
    echo "$(date) - Starting cleanup process..." >> "$SCRIPT_LOG"
    # Kill all child processes associated with FFmpeg and FIFO handling
    for PID in "${CHILD_PIDS[@]}"; do
        echo "$(date) - Terminating process PID: $PID" >> "$SCRIPT_LOG"
        kill -SIGTERM "$PID" 2>/dev/null
    done
    # Remove FIFOs
    for FIFO in "${FIFO_PATHS[@]}"; do
        if [[ -p "$FIFO" ]]; then
            echo "$(date) - Removing FIFO: $FIFO" >> "$SCRIPT_LOG"
            rm -f "$FIFO"
        fi
    done
    echo "$(date) - Cleanup process completed." >> "$SCRIPT_LOG"
    exit 0
}

# Function to handle log rotation
rotate_logs() {
    echo "$(date) - Rotating logs" >> "$SCRIPT_LOG"
    for FIFO in "${FIFO_PATHS[@]}"; do
        exec 3>&-  # Close the current log file descriptor
        exec 3>>"${LOG_FILE}"  # Reopen the new log file descriptor
        echo "$(date) - Reopened log file for ${FIFO}" >> "${LOG_FILE}"
    done
}

trap cleanup SIGINT SIGTERM
trap rotate_logs USR1

while IFS=' ' read -r CAMERA_NAME RTSP_URL; do
    [[ -z "$CAMERA_NAME" || -z "$RTSP_URL" || "$CAMERA_NAME" =~ ^# ]] && continue

    FIFO_PATH="/tmp/ffmpeg_log_${CAMERA_NAME}.fifo"
    LOG_FILE="/var/log/rtsp-to-rtmp/${CAMERA_NAME}.log"
    FIFO_PATHS+=("$FIFO_PATH")

    # Create FIFO if it does not exist
    [[ ! -p "$FIFO_PATH" ]] && mkfifo "$FIFO_PATH"

    # Start a background process to redirect FIFO output to the log file
    (
        exec 3>>"$LOG_FILE"
        while read line; do
            echo "$line" >&3
        done < "$FIFO_PATH"
    ) &
    CHILD_PIDS+=("$!")

    # Launch FFmpeg using the FIFO for logging
    (
        RETRY_DELAY=5
        MAX_RETRY_DELAY=60
        while true; do
            echo "$(date) - Starting FFmpeg for ${CAMERA_NAME}" >> "$LOG_FILE"
            ffmpeg -timeout 5000000 -i "$RTSP_URL" -acodec copy -vcodec copy -f flv "${RTMP_SERVER_URL}/${CAMERA_NAME}" > "$FIFO_PATH" 2>&1
            EXIT_STATUS=$?
            if [ $EXIT_STATUS -eq 0 ]; then
                echo "$(date) - FFmpeg exited normally for ${CAMERA_NAME}, restarting in $RETRY_DELAY seconds..." >> "$LOG_FILE"
                sleep $RETRY_DELAY
            else
                echo "$(date) - FFmpeg died unexpectedly for ${CAMERA_NAME}, restarting in $RETRY_DELAY seconds..." >> "$LOG_FILE"
                sleep $RETRY_DELAY
            fi
            [ $RETRY_DELAY -lt $MAX_RETRY_DELAY ] && RETRY_DELAY=$((RETRY_DELAY * 2))
            [ $RETRY_DELAY -gt $MAX_RETRY_DELAY ] && RETRY_DELAY=$MAX_RETRY_DELAY
        done
    ) &
    CHILD_PIDS+=("$!")
done < "$CONFIG_FILE"

wait

