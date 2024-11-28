#!/bin/bash

# Write our PID file for logrotate signal
echo $$ > /var/run/rtsp-to-rtmp.pid

CONFIG_FILE="/etc/rtsp-to-rtmp/cameras"
RTMP_SERVER_URL="rtmp://localhost/cam"
declare -a CHILD_PIDS
declare -a FIFO_PATHS
SCRIPT_LOG="/var/log/rtsp-to-rtmp/script_log"

# Unremark the below for more debugging
#exec >> "$SCRIPT_LOG" 2>&1  # Redirect all output to the script log
#set -x  # Enable debug output

# Function to handle script termination gracefully
cleanup() {
    echo "$(date) - Starting cleanup process..." >> "$SCRIPT_LOG"
    for PID in "${CHILD_PIDS[@]}"; do
        if ps -p "$PID" > /dev/null; then
            echo "$(date) - Terminating process PID: $PID" >> "$SCRIPT_LOG"
            kill -SIGTERM "$PID" 2>/dev/null
            sleep 1
            if ps -p "$PID" > /dev/null; then
                echo "$(date) - Force-killing process PID: $PID" >> "$SCRIPT_LOG"
                kill -SIGKILL "$PID" 2>/dev/null
            fi
        else
            echo "$(date) - Process PID: $PID not found, skipping..." >> "$SCRIPT_LOG"
        fi
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

    # Retry loop for FFmpeg
    (
        RETRY_DELAY=5
        MAX_RETRY_DELAY=60
        while true; do
            # Start FIFO reader process
            if [[ ! -p "$FIFO_PATH" ]]; then
                echo "$(date) - FIFO ${FIFO_PATH} missing, creating..." >> "$LOG_FILE"
                mkfifo "$FIFO_PATH"
            fi
            # Start FIFO reader
            (
                exec 3>>"$LOG_FILE"
                while read line; do
                    echo "$line" >&3
                done < "$FIFO_PATH"
            ) &
            FIFO_READER_PID="$!"
            CHILD_PIDS+=("$FIFO_READER_PID")  # Track FIFO reader process for cleanup
            echo "$(date) - Started FIFO reader with PID: $FIFO_READER_PID" >> "$LOG_FILE"

            echo "$(date) - Starting FFmpeg for ${CAMERA_NAME}" >> "$LOG_FILE"
            ffmpeg -timeout 5000000 -i "$RTSP_URL" -acodec copy -vcodec copy -f flv "${RTMP_SERVER_URL}/${CAMERA_NAME}" \
                > "$FIFO_PATH" 2>&1
            EXIT_STATUS=$?

            echo "$(date) - FFmpeg exited with status $EXIT_STATUS for ${CAMERA_NAME}" >> "$LOG_FILE"

            # Kill the FIFO reader process
            echo "$(date) - Terminating FIFO reader process PID: $FIFO_READER_PID" >> "$LOG_FILE"
            kill -SIGTERM "$FIFO_READER_PID" 2>/dev/null
            sleep 1
            if ps -p "$FIFO_READER_PID" > /dev/null; then
                echo "$(date) - Force-killing FIFO reader process PID: $FIFO_READER_PID" >> "$LOG_FILE"
                kill -SIGKILL "$FIFO_READER_PID" 2>/dev/null
            fi

            # Remove and recreate the FIFO
            if [[ -p "$FIFO_PATH" ]]; then
                echo "$(date) - Removing stale FIFO: $FIFO_PATH" >> "$LOG_FILE"
                rm -f "$FIFO_PATH"
            fi

            # Adjust retry delay and restart FFmpeg
            if [ $EXIT_STATUS -eq 0 ]; then
                echo "$(date) - FFmpeg exited normally, restarting in $RETRY_DELAY seconds..." >> "$LOG_FILE"
            else
                echo "$(date) - FFmpeg crashed, restarting in $RETRY_DELAY seconds..." >> "$LOG_FILE"
            fi
            sleep $RETRY_DELAY
            RETRY_DELAY=$((RETRY_DELAY * 2))
            [ $RETRY_DELAY -gt $MAX_RETRY_DELAY ] && RETRY_DELAY=$MAX_RETRY_DELAY
        done
    ) &
    CHILD_PIDS+=("$!")  # Track FFmpeg retry loop process for cleanup
done < "$CONFIG_FILE"

wait
