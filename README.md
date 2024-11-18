# rtmp-nginx-viewer
### RTMP html viewer based on nginx and nginx-rtmp-module

If you are looking for a lightweight way to offer a presentation for your RTMP and RTSP streams this project may assist. The project is based on the usage of cameras that support RTMP streams (and with a helper RTSP streams).

Recording for more of a nvr type setup is also possible. Largest deployment is 230+ cameras running Debian with the following hardware specifications:

** Note: Hardware specifications will vary based on each setup. **

Intel(R) Core(TM) i7-4770 CPU @ 3.40GHz.

32gb ram

2TB ssd



### General Install Instructions (Debian)
Become root user:

```sudo -i```

Install some things I find helpful in container setups and the nginx and nginx-rtmp-module:

```apt install build-essential ffmpeg vim git nginx libnginx-mod-rtmp```

Clone the repo:

```git clone https://github.com/unixabg/rtmp-nginx-viewer.git```

Change directory to the project:

```cd rtmp-nginx-viewer```

Install the project:

```touch /etc/nginx/cameras.conf```

```make install```

Add your cameras to the /etc/nginx/cameras.conf and be mindful for the camera name to use something like MyCamName or My_Cam_Name. The reason for the naming conventions is that if you want the recordings feature in proper naming will break the regex call I am making in the code to present the videos. If you have a lot of cameras best to be overly descriptive like MyDrivewayWest222, this will help provide a granular search and not overwhelm your browser.

Restart nginx:

```systemctl restart nginx```

If you want the nvr features below is the general outline. Create a the folder for video recording storage:

```mkdir -p /videos/recordings```

Set the permissions so nginx can write to the folder:

```chown -R www-data: /videos```

Edit the installed /etc/nginx/nginx.conf and unremark the recording section.
Edit the installed /etc/nginx/sites-enabled/default and unremark the /recordings location.

Install ffmpeg:

```apt install ffmpeg```

Restart nginx:

```systemctl restart nginx```

### Links
https://jared.geek.nz/2023/11/streaming-rtmp-with-openwrt/

https://www.nginx.com/

https://www.nginx.com/products/nginx/modules/rtmp-media-streaming/

