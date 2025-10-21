# rtmp-nginx-viewer
### RTMP html viewer based on nginx and nginx-rtmp-module

If you are looking for a lightweight way to offer a presentation for your RTMP and RTSP streams this project may assist. The project is based on the usage of cameras that support RTMP streams (and with a helper RTSP streams).

Recording for more of a nvr type setup is also possible. Largest deployment is 250+ cameras running Debian with the following hardware specifications:

> [!NOTE]
> Hardware specifications will vary based on each setup.


> [!NOTE]
> The addition of the 8TB ssd is for recording four days of history on
> the cameras. This system has cameras from four locations. Enabling the
> recording does put extra work on the single worker process. To address any
> potential bottleneck issues I took the liberty to put each of the four
> locations in an isolated container using
> https://tracker.debian.org/pkg/open-infrastructure-compute-tools with a bind
> to each container for storage. I have seen no performance issues with this
> configuration.


Intel(R) Core(TM) i7-4770 CPU @ 3.40GHz.

32gb ram

2TB ssd

8TB ssd

### General Install Instructions (Debian)
Become root user:

```sudo -i```

Install some things I find helpful in container setups and the nginx and nginx-rtmp-module:

```apt install build-essential ffmpeg vim git nginx libnginx-mod-rtmp curl```

Clone the repo:

```git clone https://github.com/unixabg/rtmp-nginx-viewer.git```

Change directory to the project:

```cd rtmp-nginx-viewer```

Install the project:

```make install```

Add your cameras to the /etc/nginx/cameras.conf and be mindful for the camera name to use something like MyCamName or My_Cam_Name (* use only camel or snake naming *). The reason for the naming conventions is that if you want the recordings feature an improper naming will break the regex call I am making in the code to present the videos. If you have a lot of cameras best to be overly descriptive like MyDrivewayWest222, this will help provide a granular search and not overwhelm your browser.

Restart nginx:

```systemctl restart nginx```

If you want the nvr features below is the general outline. Create a the folder for video recording and thumbnail storage:

```mkdir -p /videos/recordings```
```mkdir -p /videos/thumbnails```

> [!NOTE]
> Be careful here if you have mounted a block device to /videos
Set the permissions so nginx can write to the folder:

```chown -R www-data: /videos/recordings```
```chown -R www-data: /videos/thumbnails```

Edit the installed /etc/nginx/nginx.conf and unremark the recording section.
Edit the installed /etc/nginx/sites-enabled/default and unremark the /recordings location.

Restart nginx:

```systemctl restart nginx```

### Kiosk Feature

I have added a kiosk feature that utilizes the kiosk.html file for calling a given camera group to be displayed with something like:

```kiosk.html?cams=Camera0,Camera31,Cam7&refresh=3```

The above should compose the cameras in the specified order and in an automatic grid on the browser.

### Links
https://jared.geek.nz/2023/11/streaming-rtmp-with-openwrt/

https://www.nginx.com/

https://www.nginx.com/products/nginx/modules/rtmp-media-streaming/

