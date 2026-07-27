# mergerfs tiered storage helper

Tiered storage for the rtmp-nginx-viewer nvr features using
[mergerfs](https://github.com/trapexit/mergerfs). New recordings land on a
fast SSD, a mover script migrates finished footage to a large platter drive
on a schedule, and (optionally) on to an NFS archive after that. nginx only
ever sees the merged `/videos` path, so the recordings and history browser
work unchanged across all tiers.

```
                    /videos  (mergerfs pool - nginx reads/writes here)
                       |
      +----------------+----------------+
      |                |                |
 /mnt/cache/videos  /mnt/hdd/videos  /mnt/nfs/videos   (optional)
   SSD "hot"          HDD "cold"       NFS "archive"
   last 48h           bulk storage     long term, no-create
```

Notes on the design:

* mergerfs is a union filesystem, not a cache. It will not promote or demote
  files on its own. The included `videos-mover.sh` does the demotion on a
  cron schedule. Reads of old footage come straight off the slower tier,
  which is fine for camera playback.
* Recordings are write-once, so the mover uses **mtime**, never atime. You
  can (and should) mount everything `noatime`.
* If the SSD is also your boot drive, use a **directory** on it as the
  branch (as shown below), never `/` itself, and keep a generous
  `minfreespace` so the pool can never fill your root filesystem.

Tested on Debian with the stock `mergerfs` package:

```
apt install mergerfs
```

## 1. Directory layout

```
mkdir -p /mnt/cache/videos      # SSD branch (directory on the boot SSD)
mkdir -p /mnt/hdd               # mountpoint for the big platter drive
mkdir -p /videos                # merged view used by nginx
```

## 2. fstab

Find the HDD UUID with `lsblk -f`, then add (order matters - the HDD must
mount before the pool):

```
# big platter drive
UUID=your-hdd-uuid  /mnt/hdd  ext4  defaults,noatime,nofail  0 2

# mergerfs pool: SSD branch first, HDD branch second
/mnt/cache/videos:/mnt/hdd/videos  /videos  fuse.mergerfs  cache.files=off,category.create=ff,minfreespace=200G,fsname=videos,nofail  0 0
```

Why these options:

* `category.create=ff` - "first found": all new files and directories are
  created on the first listed branch (the SSD) as long as it has at least
  `minfreespace` available. This is what makes the SSD the write tier.
* `minfreespace=200G` - once the SSD filesystem drops below 200G free,
  mergerfs skips it and new recordings go straight to the HDD. On a shared
  boot drive this is the safety net that protects your OS. Scale to taste
  (roughly 10% of the SSD is a good floor).
* `cache.files=off` - avoids double page-caching through FUSE while nginx
  serves video files.
* `noatime` on the branch filesystems; the mover never looks at atime.

Mount everything:

```
mkdir -p /mnt/hdd/videos
mount /mnt/hdd
mount /videos
```

## 3. Directories and permissions

Ownership lives on the underlying branches, so pre-create the project
folders on **both** branch paths (not through the pool - the create policy
would put them on the SSD only) and hand them to nginx:

```
mkdir -p /mnt/cache/videos/recordings /mnt/cache/videos/thumbnails
mkdir -p /mnt/hdd/videos/recordings   /mnt/hdd/videos/thumbnails
chown -R www-data: /mnt/cache/videos /mnt/hdd/videos
```

`/videos/recordings` and `/videos/thumbnails` now appear merged and owned
by www-data. Point the recording section of `/etc/nginx/nginx.conf` and the
`/recordings` location of the site config at them exactly as in the main
project README - no nginx changes are needed for the tiering.

Thumbnails are small and hit constantly by the viewer pages, so the mover
deliberately leaves them on the SSD and only migrates `recordings/`.

## 4. The mover script

Copy `videos-mover.sh` from this folder somewhere permanent (`/opt` works
fine) and make it root-owned and executable:

```
cp helpers/mergerfs/videos-mover.sh /opt/videos-mover.sh
chown root: /opt/videos-mover.sh
chmod 755 /opt/videos-mover.sh
```

Edit the configuration block at the top:

| Variable         | Default | Meaning                                                        |
| ---------------- | ------- | -------------------------------------------------------------- |
| `SSD` / `HDD`    |         | branch paths, must match your fstab                            |
| `KEEP_HOURS`     | `48`    | hours of footage kept hot on the SSD                           |
| `FILL_LIMIT`     | `75`    | SSD usage % that triggers extra oldest-first eviction          |
| `NFS`            | empty   | optional archive branch path, empty disables stage 3           |
| `ARCHIVE_DAYS`   | `30`    | days on the HDD before footage moves to the NFS archive        |
| `RETENTION_DAYS` | empty   | days on the final tier before deletion, empty disables purging |

How the stages interact: in normal operation `KEEP_HOURS` governs and
footage moves down after 48 hours. If the cameras outpace the SSD,
`FILL_LIMIT` evicts oldest-first until usage is back under the limit. If
both of those somehow fall behind, mergerfs's own `minfreespace` overflows
new recordings to the HDD rather than filling the drive. Set the thresholds
so they trigger in that order (75% eviction fires well before a 200G
floor on any reasonably sized SSD).

Safety properties worth knowing:

* Files modified in the last 10 minutes are never touched, so an
  in-progress nginx-rtmp recording cannot be moved out from under the
  worker.
* Moves use `rsync -a --remove-source-files`, preserving www-data
  ownership and timestamps, and the source file is only removed after a
  successful copy.
* Because the destination paths mirror the source paths, a moved file
  appears at the same location in `/videos` - the camera-name regex and the
  history browser keep working, and playback URLs never change.

Dry-run it once before scheduling (on a fresh install it should find
nothing and exit immediately):

```
bash -x /opt/videos-mover.sh
```

## 5. Schedule

Cameras record continuously, so run the mover hourly. A drop-in file under
`/etc/cron.d/` keeps the schedule deployable alongside the project rather
than hidden in a personal crontab. Create `/etc/cron.d/rtmp-nginx-viewer`:

```
# run every hour for hot cache on ssd for history
0 * * * * root /opt/videos-mover.sh >> /var/log/videos-mover.log 2>&1
```

cron.d gotchas, all of which cause the job to be silently skipped:

* Unlike a user crontab, cron.d entries **require a user field** (`root`
  above) between the schedule and the command.
* The file must be owned by root and not group/world-writable:
  `chown root: /etc/cron.d/rtmp-nginx-viewer` and `chmod 644` it.
* On Debian, cron ignores cron.d filenames containing dots, so do not name
  the file something like `mover.sh`.

No reload is needed; cron picks up cron.d changes automatically. Verify at
the next top of the hour:

```
grep videos-mover /var/log/syslog | tail
```

You should see a `CRON` line with `(root) CMD (/opt/videos-mover.sh ...)`.

After the first `KEEP_HOURS` have elapsed, check
`/var/log/videos-mover.log` and confirm files are appearing under
`/mnt/hdd/videos/recordings` while `/videos/recordings` looks unchanged
from the browser.

## 6. Optional: NFS archive tier

Any directory can be a branch, including an NFS mount. Add the share to
fstab **before** the pool line:

```
nas:/export/archive  /mnt/nfs  nfs  defaults,noatime,nofail,soft,timeo=150,retrans=3  0 0
```

`soft` with sane timeouts matters here: if the NAS drops offline you want
reads of archived footage to error out, not hang nginx workers serving the
live view.

Then extend the pool line with the archive branch marked **no-create**:

```
/mnt/cache/videos:/mnt/hdd/videos:/mnt/nfs/videos=NC  /videos  fuse.mergerfs  cache.files=off,category.create=ff,minfreespace=200G,fsname=videos,nofail  0 0
```

The `=NC` suffix guarantees mergerfs never places new files on the archive
regardless of policy or how full the other tiers get; existing files on it
remain fully readable through `/videos`. Create the target directory and
set the `NFS=` variable in the mover script:

```
mkdir -p /mnt/nfs/videos/recordings
```

```
NFS=/mnt/nfs/videos
```

The mover then adds a stage that migrates recordings older than
`ARCHIVE_DAYS` from the HDD to the NAS, and skips the stage cleanly (with a
log warning) whenever the share is not mounted.

Ownership note: `rsync -a` preserves www-data, which only maps correctly if
the NAS export either uses the same UID for www-data or squashes ownership.
On a Debian-ish NAS, `all_squash,anonuid=33,anongid=33` on the export is
the simple fix if archived listings show the wrong owner.

## 7. Retention

The final tier fills eventually. Set `RETENTION_DAYS` in the mover script
to delete footage older than N days from the last tier in the chain (the
NFS archive when configured, otherwise the HDD). Size it from your real
numbers: total daily footage is roughly

```
cameras x bitrate(Mbps) / 8 x 86400 / 1000  GB per day
```

e.g. ten cameras at 4 Mbps produce about 430 GB/day - roughly six weeks on
a 20TB drive.

## Troubleshooting

* **`mkdir /videos/foo` only shows up on the SSD branch** - expected; the
  `ff` create policy applies to directories too. The pool view is the
  union, so it still appears in `/videos` correctly.
* **Pool refuses to mount** - both branch paths must exist before mount,
  and the HDD (and NFS) fstab lines must come before the pool line.
* **Wrong ownership on migrated files** - run the mover as root (cron as
  root does), and see the NFS ownership note above for the archive tier.
* **Which drive is a file actually on?** - `getfattr -n user.mergerfs.relpath`
  works, or simply `ls /mnt/*/videos/recordings/ | grep <file>`.
* **USB-attached branch drives** - work fine, but use UASP-capable
  enclosures, mount by UUID, keep `nofail`, and avoid hubs. Prefer SATA for
  the always-recording tiers and keep USB for the cold end.
