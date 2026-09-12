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

If you want the nvr features below is the general outline. `make install`
creates /videos/recordings, /videos/thumbnails and /videos/detections for
you, and the /recordings and /detections locations are enabled in
/etc/nginx/sites-enabled/default, so all that is left is nginx.conf.

> [!NOTE]
> Be careful here if you have mounted a block device to /videos

Set the permissions so nginx can write to the folders:

```chown -R www-data: /videos/recordings```
```chown -R www-data: /videos/thumbnails```

Edit the installed /etc/nginx/nginx.conf and unremark the recording section.
While you are there, unremark the exec_record_done line that calls
/opt/nginx-thumbs if you want the per-camera thumbnails that the Camera
Status page displays.

Restart nginx:

```systemctl restart nginx```

#### Navigation adapts to what is enabled

The home page shows four icons: Live View, History, Camera Status and
Detections. Only Live View works without recording — History lists the
recordings, the Camera Status thumbnails are written by the
exec_record_done hook when a segment closes, and the detector reads the
recordings. Rather than linking to pages that would be blank, index.html
asks the server what is actually there and shows only those entries:

| Icon | Shown when |
| --- | --- |
| Live View | always |
| History | /recordings/ lists at least one .mp4 |
| Camera Status | /thumbnails/ lists at least one .jpg |
| Detections | /detections/index/days.json reports at least one day |

The test is for content rather than for the directory, because `make
install` creates all three directories whether or not you use them. So an
icon appears when the feature starts producing output — a freshly enabled
recording setup gains its History icon when the first segment closes — and
nothing has to be configured to make this work.

If a path is behind auth_basic and the browser has not authenticated yet,
the probe sees a 401 and shows the icon rather than hiding it, so the link
is still there to authenticate through.

#### Naming your site

The home page reads /var/www/html/sitename.txt and uses the first line as
the welcome message and the browser tab title:

```echo "North Depot" > /var/www/html/sitename.txt```

`make install` creates it with a default only when it does not already
exist, and `make upgrade` leaves it alone, so the name survives upgrades.
Delete the file to go back to the default. `make uninstall` keeps it;
`make purge` removes it.

#### Upgrading an existing install

```git pull```
```make upgrade```

`make upgrade` refreshes the web pages and the vendored JS/CSS. It
deliberately leaves your nginx configuration alone, since those files are
where your cameras and local choices live and overwriting them would be
worse than leaving a feature unconfigured.

That means location blocks added in a newer release are not picked up
automatically. If an icon you expect never appears, compare your
/etc/nginx/sites-available/default against the one in the repo and merge
in what is missing — /recordings and /detections are the two that the
navigation depends on. Nothing breaks in the meantime; the probe simply
finds nothing at those paths and the icon stays hidden.

```diff -u /etc/nginx/sites-available/default default```

#### Securing an exposed install

The /recordings, /detections and /thumbnails locations use nginx autoindex,
which means anyone who can reach the server can browse a list of every
recording and every detection thumbnail. That is fine on an isolated
network and not fine on anything reachable from outside. For those, put
TLS in front of it and add basic auth to the server block:

```
auth_basic "Restricted";
auth_basic_user_file /etc/nginx/.htpasswd;
```

Create the file with `htpasswd -c /etc/nginx/.htpasswd <user>` from the
apache2-utils package.

### Detection helper (optional)

helpers/detector adds object detection over the completed recordings: it
runs a YOLO model across sampled frames, writes one JSON manifest and a
few thumbnails per recording, and serves a browsable page of what was
found at /detections/. It runs from cron, low-priority, so recording
always wins, and it works on hardware from a Raspberry Pi to a CUDA node —
a mixed cluster can share one recordings tree over NFS.

See helpers/detector/README.md for installation, per-camera rules, and the
performance notes. Once it has written its first manifests, the Detections
icon appears in the navigation on its own.

### Kiosk Feature

I have added a kiosk feature that utilizes the kiosk.html file for calling a given camera group to be displayed in an automatic grid on the browser. Point a kiosk URL at a group of cameras like so:

```kiosk.html?cams=Camera0,Camera31,Cam7&refresh=120```

The cameras are composed in the order specified and the grid arranges itself based on how many cameras you list.

The URL accepts two parameters:

- `cams` — a comma-separated list of cameras to display, in order.
- `refresh` — optional, the number of **seconds** between full player rebuilds. The rebuild reconnects every stream so a kiosk can recover from a camera that dropped and came back. If omitted it defaults to 300 (5 minutes).

By default each entry in `cams` is pulled from the `cam` application, so `Camera0` resolves to `hls/Camera0.m3u8`. You may also target a specific application by prefixing the entry with the application name and a colon (see the sub-stream section below):

```kiosk.html?cams=cam-sub:Camera0,cam-sub:Camera31,Cam7&refresh=120```

In the example above `Camera0` and `Camera31` are pulled from the lower-resolution `cam-sub` application while `Cam7` (no prefix) is pulled from the default `cam` application. The overlay label on each tile uses just the camera name.

> [!NOTE]
> Large camera resolutions are expensive for the kiosk machine to decode. Each tile is a full decode pipeline in the browser, so a wall of high-resolution streams can make the viewing station sluggish even though each tile is small on screen. If you are running grids of more than a handful of cameras, use the sub-stream application below so the kiosk decodes small streams instead of full-resolution main streams.

### Sub-stream Application for Kiosk Grids

To keep the kiosk machine responsive I run a second RTMP application named `cam-sub` that carries lower-resolution streams just for grid viewing. The main `cam` application is left for recording and single-camera live views, while `cam-sub` feeds the kiosk. Where a camera offers a native sub-stream (most do), pulling it is far cheaper than asking the server to transcode.

The pieces that make this work are already in the project files:

- `nginx.conf` defines the `cam-sub` application with `hls_path /tmp/sub-hls` and includes `cameras-sub.conf`.
- The `default` site serves the sub-stream HLS at the `/sub-hls` location.
- `cameras-sub.conf` holds the low-resolution pull lines for `cam-sub`.

Add your low-resolution pulls to /etc/nginx/cameras-sub.conf. Use the **same** `name=` value as the matching entry in cameras.conf so a camera is reachable as both `cam/<name>` (main) and `cam-sub/<name>` (sub):

```#pull rtmp://ipAddress/bcs/channel0_sub.bcs?channel=0&stream=0&user=admin&password=yourPassword name=CamName static;```

Restart nginx:

```systemctl restart nginx```

You can confirm a sub-stream is reachable by loading its playlist directly in a browser before adding it to a kiosk grid:

```http://your-server/sub-hls/CamName.m3u8```

> [!NOTE]
> The sub-stream HLS segments are written to /tmp/sub-hls. If you have any housekeeping that cleans /tmp, make sure it does not sweep the live segment directories out from under nginx.

### Links
https://jared.geek.nz/2023/11/streaming-rtmp-with-openwrt/

https://www.nginx.com/

https://www.nginx.com/products/nginx/modules/rtmp-media-streaming/

