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
  files on its own. The included `videos-mover` does the demotion on a
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
mount /mnt/hdd && mkdir -p /mnt/hdd/videos && mount /videos \
  && echo "All mounted OK" || echo "Mount failed — check dmesg and /etc/fstab"
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

Copy `videos-mover` from this folder somewhere permanent (`/opt` works
fine) and make it root-owned and executable:

```
cp helpers/mergerfs/videos-mover /opt/videos-mover
chown root: /opt/videos-mover
chmod 755 /opt/videos-mover
```

You should not need to edit it. Every setting reads from the environment
first and falls back to the default baked into the script, so the tuning
lives in `/etc/cron.d/rtmp-nginx-viewer` (section 5) rather than in
`/opt/videos-mover`. That keeps the deployed script byte-identical to the
one in git - safe to overwrite on upgrade, and easy to diff across a fleet.

| Variable         | Default | Meaning                                                        |
| ---------------- | ------- | -------------------------------------------------------------- |
| `SSD` / `HDD`    |         | branch paths, must match your fstab                            |
| `SSD_HOURS`      | `48`    | age at which footage moves off the SSD to the HDD              |
| `FILL_LIMIT`     | `75`    | SSD usage % that triggers extra oldest-first eviction          |
| `NFS`            | empty   | optional archive branch path, empty disables the archive tier  |
| `HDD_DAYS`       | empty   | age at which footage leaves the HDD - moved to NFS when `NFS` is set, **deleted** when it is not |
| `NFS_DAYS`       | empty   | age at which footage is **deleted** from the NFS archive       |
| `LOCKFILE`       | `/run/videos-mover.lock` | single-instance lock, empty disables locking |

**One setting per tier, each meaning the same thing: how old footage may
get on that tier before the next stage takes it.** The last tier in your
chain is the one that deletes - `HDD_DAYS` without an archive, `NFS_DAYS`
with one. Leave the deleting one empty and nothing is ever purged; the
mover says so once per run rather than letting the disk fill quietly.

**All three are ages since the recording was made**, not time spent on
that tier. Moves use `rsync -a`, which preserves mtime, so a file carries
its original timestamp across every tier. `HDD_DAYS=30` means "30 days
old" - the hours it spent on the SSD are part of that 30 days, not extra,
so the setting is also your total days of footage when there is no
archive.

With an archive, `NFS_DAYS` must be **greater** than `HDD_DAYS`: the first
is when a file leaves the HDD, the second is when it is deleted, and both
are measured from the same origin. Set them equal and a file would be
copied to the archive and deleted on the same run. The mover refuses to
start rather than do that.

A worked example - 36 hours hot, a month on the HDD, a year archived:

```
SSD_HOURS=36     # recordings move to the HDD after 36 hours
HDD_DAYS=30      # and on to the NFS archive at 30 days old
NFS_DAYS=365     # and are deleted at a year old
```

Without the archive, drop `NFS`/`NFS_DAYS` and `HDD_DAYS=30` is simply
"keep 30 days of footage".

The numeric settings are validated before anything moves or deletes. A
non-numeric value, a `FILL_LIMIT` outside 1-99, a tier set to `0`, or an
`NFS_DAYS` that is not greater than `HDD_DAYS` aborts the run with an
error in the log instead of acting on it - worth having now that the
values live in a file cron parses rather than in the script itself. Each
run also logs a `config:` line with the values it used,
so the log shows what was in effect at the time.

Stage order: the HDD is emptied before it is filled. Whatever `HDD_DAYS`
sends away - deleted, or moved to the archive - goes first, and only then
does footage move down from the SSD. Nothing checks free space before a
move and `rsync` failures are not inspected, so this ordering is what
keeps a nearly-full HDD working: the space only has to exist for the
difference between what arrives and what left, rather than for both at
once. The cost is one cycle of latency, since a file crossing `HDD_DAYS`
mid-run is handled on the next one. If the HDD is still 95% or more full
after the purge, the mover logs a warning - `rsync --remove-source-files`
only unlinks a source after a successful transfer, so a full HDD strands
recordings on the SSD rather than losing them, but it does so quietly.

How the stages interact: in normal operation `SSD_HOURS` governs and
footage moves down after 48 hours. If the cameras outpace the SSD,
`FILL_LIMIT` evicts oldest-first until usage is back under the limit. If
both of those somehow fall behind, mergerfs's own `minfreespace` overflows
new recordings to the HDD rather than filling the drive. Set the thresholds
so they trigger in that order (75% eviction fires well before a 200G
floor on any reasonably sized SSD).

Safety properties worth knowing:

* **Only one mover runs at a time.** The script re-runs itself under
  `flock -n`, so if a pass is still working through a backlog when cron
  fires again, the new run logs a line and exits 0 instead of starting a
  second mover against disks that are already saturated. This matters most
  on the first run after enabling the tiering, and any time a large
  eviction or archive stage overruns the hour. Exit code 0 on a skip keeps
  cron from mailing you about it.
* If a branch directory is missing - usually a failed mount leaving
  mergerfs on a degraded pool - the mover logs an error and exits 1 rather
  than migrating recordings onto the root filesystem.
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
bash -x /opt/videos-mover
```

## 5. Schedule

Cameras record continuously, so run the mover hourly. A drop-in file under
`/etc/cron.d/` keeps the schedule deployable alongside the project rather
than hidden in a personal crontab.

cron.d files can carry environment assignments as well as schedules, and
the mover reads its settings from the environment. So this one file holds
both the schedule and the tuning, and `/opt/videos-mover` never gets
edited. Create `/etc/cron.d/rtmp-nginx-viewer`:

```
# rtmp-nginx-viewer - tiered storage mover
# Settings below are read by /opt/videos-mover. Anything left out uses the
# script's built-in default. See helpers/mergerfs/README.md section 4.

#SSD=/mnt/cache/videos
#HDD=/mnt/hdd/videos
SSD_HOURS=48
FILL_LIMIT=75

# How many days of footage to keep. Without the NFS tier below this is
# the delete threshold, so leaving it commented out means nothing is ever
# purged and the HDD fills. Do not set it to 0.
#HDD_DAYS=30

# Uncomment to enable the NFS archive tier (section 6). HDD_DAYS then
# becomes when footage MOVES to the archive, and NFS_DAYS is when it is
# deleted - NFS_DAYS must be the larger of the two.
#NFS=/mnt/nfs/videos
#NFS_DAYS=365

# run every hour for hot cache on ssd for history
0 * * * * root /opt/videos-mover >> /var/log/videos-mover.log 2>&1
```

Changing retention is now a one-line edit here, with no reload needed -
cron re-reads the file on the next tick, and the mover logs the values it
picked up on each run.

Environment lines in cron.d have their own rules:

* Assignments apply to **every** job in that same file, so if you add other
  entries later, keep in mind they inherit these too.
* No shell expansion. `SSD_HOURS=$FOO` is the literal string `$FOO`, not a
  variable reference, and the mover will reject it as non-numeric.
* Write values bare - `SSD_HOURS=48`, not `SSD_HOURS="48"`. Debian's cron
  does strip matching quotes, but bare values avoid the question entirely.
* An assignment must come **before** the job line to apply to it.
* A commented-out assignment simply falls back to the script default, which
  is why the optional tiers above are safe to leave commented.

cron.d gotchas, all of which cause the job to be silently skipped:

* Unlike a user crontab, cron.d entries **require a user field** (`root`
  above) between the schedule and the command.
* The file must be owned by root and not group/world-writable:
  `chown root: /etc/cron.d/rtmp-nginx-viewer` and `chmod 644` it.
* On Debian, cron ignores cron.d filenames containing dots, so do not name
  the file something like `mover`.

No reload is needed; cron picks up cron.d changes automatically. Verify at
the next top of the hour:

```
grep videos-mover /var/log/syslog | tail
```

You should see a `CRON` line with `(root) CMD (/opt/videos-mover ...)`.

After the first `SSD_HOURS` have elapsed, check
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
remain fully readable through `/videos`. Create the target directory:

```
mkdir -p /mnt/nfs/videos/recordings
```

Then uncomment the archive settings in `/etc/cron.d/rtmp-nginx-viewer`:

```
NFS=/mnt/nfs/videos
NFS_DAYS=365
```

The mover then adds a stage that migrates recordings older than
`HDD_DAYS` from the HDD to the NAS, and skips the stage cleanly (with a
log warning) whenever the share is not mounted.

Note what enabling this does to `HDD_DAYS`: without an archive it is a
delete threshold, with one it becomes a move threshold, and deletion
passes to `NFS_DAYS`. So turning on the archive tier on a node that was
keeping 30 days gives you 30 days on the HDD plus however long `NFS_DAYS`
allows - and if you leave `NFS_DAYS` unset, the archive grows forever.

Ownership note: `rsync -a` preserves www-data, which only maps correctly if
the NAS export either uses the same UID for www-data or squashes ownership.
On a Debian-ish NAS, `all_squash,anonuid=33,anongid=33` on the export is
the simple fix if archived listings show the wrong owner.

## 7. Retention

The last tier fills eventually. Whichever it is - the HDD on a two-tier
node, the archive when one is configured - set that tier's variable in
`/etc/cron.d/rtmp-nginx-viewer` and the mover deletes footage older than
that many days:

```
HDD_DAYS=30      # two tiers: keep 30 days, then delete
NFS_DAYS=365     # three tiers: HDD_DAYS moves, this deletes
```

Leave it unset to disable purging entirely - the mover logs a `NOTE:`
line each run saying nothing is being deleted, so an unbounded disk is
visible in the log before it is visible as a failure. Do not set it to
`0`, which the mover rejects because it would sweep out almost
everything.
Size it from your real numbers: total daily footage is roughly

```
cameras x bitrate(Mbps) / 8 x 86400 / 1000  GB per day
```

e.g. ten cameras at 4 Mbps produce about 430 GB/day - roughly six weeks on
a 20TB drive.

### Migrating from the old variable names

`KEEP_HOURS`, `ARCHIVE_DAYS` and `RETENTION_DAYS` still work and are
mapped automatically, with a warning in the log naming what each became.
The rename happened because `RETENTION_DAYS` meant a different tier
depending on whether `NFS` was set - "delete from whichever tier happens
to be last" - so the same line in the same file meant the HDD on one node
and the archive on another.

| old | new, no archive | new, with archive |
| --- | --- | --- |
| `KEEP_HOURS` | `SSD_HOURS` | `SSD_HOURS` |
| `ARCHIVE_DAYS` | (was ignored) | `HDD_DAYS` |
| `RETENTION_DAYS` | `HDD_DAYS` | `NFS_DAYS` |

Note `ARCHIVE_DAYS` used to default to `30` even on nodes with no archive,
where it did nothing. `HDD_DAYS` has no default, so a two-tier node must
now set it deliberately to have anything deleted - which is the point.

**Avoid a retention collision:** the stock project crontab ships its own
nightly purge along the lines of

```
#0 2 * * *  www-data  /usr/bin/find /videos/recordings -type f -mtime +30 -delete
```

Use one retention mechanism, not both. Note both now live in the same
file, so the conflict is easy to spot: when you uncomment `HDD_DAYS`,
comment out that `find` line - if both are active, the shorter
value silently wins and footage disappears earlier than either setting
suggests. (Consolidating into the mover is recommended: one script, one
log, and each purged file is logged with a `purge:` line.)

Never add any cleanup for `/videos/thumbnails`. The mover deliberately
leaves thumbnails alone: a stale thumbnail whose mtime has stopped updating
is used as the artifact showing when a camera went down.

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
* **A setting in cron.d seems to be ignored** - check the `config:` line the
  mover logs on each run; it shows the values actually in effect. Common
  causes: the assignment sits below the job line, it is in a different
  cron.d file, or the value has a stray space (`SSD_HOURS = 48` is not a
  valid cron assignment). To test a value without waiting for cron:
  `SSD_HOURS=1 /opt/videos-mover`.
* **Log shows "another videos-mover is still running"** - a previous run
  overran the cron interval. Occasional lines are normal after enabling
  tiering or during a big archive pass. Continuous lines mean the mover
  can never finish in an hour: check whether the HDD is the bottleneck
  (`iostat -x 5`), lower `FILL_LIMIT` so evictions are smaller and more
  frequent, or move the archive stage to its own nightly schedule.
* **Mover never runs, no log output at all** - a stale lock cannot cause
  this (`flock` releases on process exit, including a kill or reboot), so
  look at cron first. To confirm nothing holds the lock:
  `flock -n /run/videos-mover.lock true && echo free`.
* **USB-attached branch drives** - work fine, but use UASP-capable
  enclosures, mount by UUID, keep `nofail`, and avoid hubs. Prefer SATA for
  the always-recording tiers and keep USB for the cold end.
