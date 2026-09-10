# nvr-backup

Pull-based configuration backup for an `rtmp-nginx-viewer` fleet.

Runs from your workstation. Nothing is installed on the NVR servers — for each
host it opens one SSH connection, tars the config paths remotely, and streams
the archive straight back to local disk. Nothing is staged on the NVR, which
matters when those boxes are already I/O constrained by recording.

---

## What gets backed up

**Core viewer**

| Path | Notes |
|---|---|
| `/etc/nginx` | Directory. `nginx.conf`, `cameras.conf`, `sites-available/` |
| `/etc/rtsp-to-rtmp` | Directory. Camera pull definitions |
| `/etc/cron.d/rtmp-nginx-viewer` | Recording housekeeping, mover schedule, **and the mover's tuning** — see below |
| `/etc/fstab` | mergerfs branch config and tier layout |

**mergerfs helper**

| Path | Notes |
|---|---|
| `/opt/videos-mover` | Tier mover script. Stock — kept only to record which version a host was running |
| `/etc/logrotate.d/videos-mover` | Mover log rotation |

`videos-mover` reads every setting from the environment, so its tuning
(`KEEP_HOURS`, `FILL_LIMIT`, `NFS`, `ARCHIVE_DAYS`, `RETENTION_DAYS`) lives as
environment assignments in `/etc/cron.d/rtmp-nginx-viewer`, not in the script.
That one file is therefore the whole mergerfs configuration for a host, and
`/opt/videos-mover` should be byte-identical to git on every server.

A practical consequence: a fleet-wide `diff` across the archives will show
`/opt/videos-mover` identical everywhere and any real divergence in the cron.d
file. If a host's mover *does* differ, that's a signal — either it missed an
upgrade or someone edited it in place.

**detector helper**

| Path | Notes |
|---|---|
| `/opt/detection` | `cameras.json` per-camera rules, plus any `env-*.sh` wrappers. Venv and models excluded, see below |
| `/etc/detection` | `nodes.txt` for multi-node runs |
| `/etc/cron.d/detection` | Sweep + nightly prune schedule written by `make install-cron` |
| `/etc/exports` | Read-only NFS export of recordings to the detection nodes |

Paths that don't exist on a given host are silently skipped, so the same
script works across a viewer box, a detection box, and servers at different
stages of rollout. `nvr-backup -p` prints the list from whatever copy is
installed.

To change it, edit the `REMOTE_PATHS` array near the top of the script.

### What is deliberately excluded

`/opt/detection` holds a multi-gigabyte torch venv and exported model
directories sitting right next to the config worth keeping, so the remote
`tar` runs with an exclude list (`TAR_EXCLUDES` in the script):

```
venv  __pycache__  *.pyc  *.pt  *.onnx  *_openvino_model  *_ncnn_model  *.log  *.log.*
```

Patterns are unanchored, so they match at any depth under any backed-up path.
Everything excluded here is reproducible from the repo: `make install-deps`
rebuilds the venv and `make model` re-exports the accelerated model.

Also not included: recordings, thumbnails, and detection output
(`/var/detections`, `/videos/detections`). This backs up configuration only —
enough to rebuild a server, not to restore footage. Detection manifests and
thumbnails are regenerable by re-running the worker over the recordings.

As a backstop, any archive coming back at or above `WARN_MB` (default 16) is
flagged in the output. A config-only archive is normally well under a
megabyte, so a warning means a path is pulling in data it shouldn't.

---

## Install

```bash
make install-user     # ~/.local/bin, no sudo
make config           # scaffold ~/.config/nvr-backup/hosts
```

Or system-wide:

```bash
sudo make install     # /usr/local/bin
make config
```

`make install` runs `make check` first, so a syntax error can't get installed.
`install-user` warns if the target directory isn't on your `PATH`.

Other targets:

| Target | Does |
|---|---|
| `make help` | List targets |
| `make check` | `bash -n` plus `shellcheck` if installed |
| `make config` | Create a commented sample hosts file (won't overwrite) |
| `make version` | Print the version embedded in the script |
| `make tag` | Annotated git tag from `VERSION` — refuses a dirty tree or duplicate tag |
| `make uninstall` / `make uninstall-user` | Remove the script; config and backups are left alone |

Manual install works too if you'd rather skip make:

```bash
install -m 755 nvr-backup ~/.local/bin/nvr-backup
mkdir -p ~/.config/nvr-backup
```

### Hosts file

Create `~/.config/nvr-backup/hosts` with one host per line. Blank lines and
`#` comments are ignored. Anything `ssh` accepts works here, including
aliases from `~/.ssh/config`:

```
# NVR fleet
your-server1
your-server2
your-server3
user@your-server
your-server5.example.com
```

Using `~/.ssh/config` aliases is usually cleaner than putting usernames and
FQDNs in this file:

```
# ~/.ssh/config
Host your-server*
    User user
    IdentityFile ~/.ssh/id_ed25519_nvr
```

### Optional config file

`~/.config/nvr-backup/config` is sourced if present:

```bash
BACKUP_ROOT=/srv/backups/nvr
KEEP=30
JOBS=8
WARN_MB=16
SSH_OPTS="-o ConnectTimeout=30 -o BatchMode=yes"
```

Any of these can also be set as environment variables for a single run.

---

## Server-side prerequisite

The remote `tar` needs to read root-owned config, so each server needs a
passwordless sudo rule. The script uses `sudo -n`, which fails immediately
rather than hanging on a password prompt.

```
# /etc/sudoers.d/nvr-backup   (mode 0440, on each server)
youruser ALL=(root) NOPASSWD: /usr/bin/tar
```

Validate before saving:

```bash
sudo visudo -cf /etc/sudoers.d/nvr-backup
```

If you'd rather not grant blanket `tar` rights, an alternative is to make the
config paths group-readable by an operator group and drop the `sudo -n` from
the script's ssh command.

---

## Usage

```bash
nvr-backup                  # every host in the hosts file, 4 at a time
nvr-backup your-server3            # only the named host(s)
nvr-backup your-server1 your-server4      # several
nvr-backup -j 8             # 8 hosts in parallel
nvr-backup -n               # dry run — show what would happen
nvr-backup -l               # list configured hosts and exit
nvr-backup -p               # list the paths and excludes, then exit
nvr-backup -f other-hosts   # use a different hosts file
nvr-backup -V               # print version and exit
nvr-backup -h               # help
```

Output:

```
11:05:08 nvr-backup 1.1.0 — backing up 8 host(s), 4 at a time -> /home/youruser/backups/nvr
11:05:12 OK   your-server1 — 88K -> /home/youruser/backups/nvr/your-server1/your-server1-20260803-110508.tar.gz
11:05:12 OK   your-server2 — 91K -> /home/youruser/backups/nvr/your-server2/your-server2-20260803-110508.tar.gz
...
11:05:19 All 8 host(s) backed up.
```

Archives land in `$BACKUP_ROOT/<host>/<host>-<timestamp>.tar.gz`, mode 0600,
pruned to the most recent `KEEP` per host.

Exit code is `0` only if every host succeeded, so a cron or CI wrapper will
actually notice failures.

---

## SSH keys and the agent

The default `SSH_OPTS` includes `BatchMode=yes`. This makes unreachable hosts
fail fast instead of hanging a parallel job on a password prompt — but it also
means **SSH will never prompt you to unlock a passphrase-protected key**.

Load the key into your agent first:

```bash
ssh-add ~/.ssh/id_ed25519_nvr    # prompts once
ssh-add -l                       # confirm it's loaded
nvr-backup
```

If no agent is running:

```bash
eval "$(ssh-agent -s)"
ssh-add ~/.ssh/id_ed25519_nvr
```

For persistence across logins, use `keychain`, your desktop keyring, or
`AddKeysToAgent yes` in `~/.ssh/config`.

### Running nvr-backup from inside an SSH session

If you SSH into your workstation and run `nvr-backup` there, the script has no
agent to talk to unless you forwarded one. Either:

- connect with `ssh -A workstation` to forward your local agent, or
- start an agent and load the key inside that session, or
- run it from a local terminal / `tmux` session that already has the agent

`echo $SSH_AUTH_SOCK` is the quick check — empty means no agent is reachable.

---

## Restoring

Archives use absolute paths, so they extract back to their original locations:

```bash
# inspect first — note member names keep their leading slash
tar -tzf your-server1-20260803-110508.tar.gz

# restore a single file (the leading / is required to match the member name)
sudo tar -xzf your-server1-20260803-110508.tar.gz -C / /etc/nginx/cameras.conf

# full restore
sudo tar -xzf your-server1-20260803-110508.tar.gz -C /
sudo systemctl reload nginx
```

`tar` prints `Removing leading '/' from member names` on extraction; that's
normal and combines with `-C /` to put files back where they came from. Drop
the leading slash from the member name and tar reports `Not found in archive`
and exits 2 — easy to miss if you're not checking the exit code.

Review `/etc/fstab` by hand before overwriting it — disk UUIDs are
host-specific and restoring another server's fstab will leave the box
unbootable. The same caution applies to `/etc/exports`, where the allowed
subnet is usually site-specific.

Restoring `/opt/detection` brings back `cameras.json` and any wrapper scripts,
but not the venv or the model — those were excluded on purpose. Rebuild them
from the detector helper:

```bash
cd helpers/detector
make install-deps          # venv + torch/ultralytics
make model                 # re-export the accelerated model
make doctor                # verify
```

`make install-config` will not clobber a restored `cameras.json`, so the order
doesn't matter.

For the mergerfs tier, restoring `/etc/cron.d/rtmp-nginx-viewer` restores both
the schedule and the mover's tuning in one file. `/opt/videos-mover` itself is
better re-copied from git than from the archive, so the host ends up on the
current version rather than whatever it was running when the backup ran. Check
the `config:` line in `/var/log/videos-mover.log` after the first run to
confirm the restored values took effect.

---

## Scheduling

On your workstation, not the servers:

```bash
# crontab -e
0 4 * * *  /home/youruser/.local/bin/nvr-backup >> /home/youruser/.local/log/nvr-backup.log 2>&1
```

Cron has no SSH agent, so use a dedicated passphrase-less key restricted to
this purpose, or a systemd user timer that inherits your keyring session.

For a passphrase-less key, lock it down on the server side:

```
# ~/.ssh/authorized_keys on each NVR
restrict,command="/usr/bin/sudo -n tar -czf - --absolute-names /etc/nginx /etc/fstab ..." ssh-ed25519 AAAA...
```

---

## Troubleshooting

**`Permission denied (publickey)`**
Test the connection directly: `ssh -v youruser@host true`. Usually a wrong
username in the hosts file, a key not loaded in the agent, or no agent in the
current session. See the SSH section above.

**`sudo: a password is required`**
The `/etc/sudoers.d/nvr-backup` rule is missing or the username in it doesn't
match the connecting user.

**`empty archive`**
None of the `REMOTE_PATHS` existed on that host, or the remote `tar` failed
silently. Run `ssh host 'ls -la /etc/nginx'` to confirm.

**`archive is NNMB, over the threshold`**
Something under a backed-up path is bulk data rather than config. List the
archive to find it — `tar -tzf archive.tar.gz | head -50` — then either add a
pattern to `TAR_EXCLUDES` or narrow the entry in `REMOTE_PATHS`. On a detection
box the usual cause is a model export whose directory name doesn't end in
`_openvino_model` or `_ncnn_model`.

**Hangs on one host**
Lower `ConnectTimeout` in `SSH_OPTS`, or run with `-j 1` to see which host
stalls.

---

## Versioning

The version lives in one place — the `VERSION` variable at the top of
`nvr-backup`. `make version` reads it back, and `nvr-backup -V` reports it from
whatever copy is actually installed, which is what you want when someone pastes
output into an issue.

Releases are git tags. Bump `VERSION`, commit, then:

```bash
make tag                    # creates v<VERSION>, refuses if the tree is dirty
git push origin v1.1.0
```

The version also appears in the run header line, so scheduled-backup logs
record which version produced them.

---

## Design notes

Pull-based rather than push-based, which means:

- one script to maintain instead of eight installs to keep in sync
- no root SSH keys distributed across the fleet
- no backup staging on NVR disks that are already saturated
- adding a server is one line in the hosts file
