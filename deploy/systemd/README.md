# Clonoth systemd units

This directory contains optional systemd unit templates for a Debian deployment under `/opt/Clonoth`.

## Multi-instance templates

`kazebot@.service` and `kazebot-qq@.service` run one bot per instance, all sharing a single checkout; platform accounts such as the QQ login belong to the instance. The instance name is the instance id (a lowercase slug, or the QQ number for instances created before ids existed); the workspace is `/opt/kazebot-data/<id>`. `deploy/new_instance.sh <id> [label] [qq]` lays out the workspace and hands the rest to `kazebot-provision@<id>`, which installs and enables these units. To do it by hand:

```bash
sudo cp deploy/systemd/kazebot@.service deploy/systemd/kazebot-qq@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kazebot@<id> kazebot-qq@<id>
```

See `deploy/MULTI-ACCOUNT.md` for ports, NapCat containers, and the shared console.

## Host cleanup timer

NTQQ/NapCat keep their images, videos and caches inside the NapCat containers, owned by root. The per-instance cleanup runs as `kazebot` in a sandbox and cannot delete them, so on kazebot deployments a single root timer cleans them for every instance on the machine: `kazebot-host-cleanup.timer` runs `kazebot-host-cleanup.service` once a day.

The service runs `/usr/local/lib/kazebot/qq_cache.py` with `/usr/bin/python3 -I`. That file is a root-owned copy of `adapters/onebot/qq_cache.py`, which uses only the standard library: `/opt/kazebot` and its `.venv` belong to `kazebot`, and running anything from there as root would hand the machine to the agent. `bootstrap.sh` and `install_provision.sh` install the copy and the units; `deploy.sh` refreshes the copy on every update.

Running as root, it only follows root-owned processes through `/proc/<pid>/root`, because any user can start a process whose command line mentions napcat. It also scans the host directories the deploy scripts mount into the containers, `/opt/napcat/qq` and `/opt/napcat-<instance>/qq`, so stopped containers are cleaned too; archived `*.deleted-*` directories are left alone.

Instance cleanups skip this task: `kazebot-cleanup@.service` passes `--scope workspace`, and `kazebot.service` / `kazebot@.service` set `CLONOTH_MAINTENANCE_SCOPE=workspace` for the cleanup the engine schedules itself. Without either, `engine.data_cleanup` still runs every task, as the legacy `clonoth-data-cleanup.service` below does.

It never reads a workspace `.env`. Override the retention or add cache roots with a drop-in:

```bash
sudo systemctl edit kazebot-host-cleanup.service
# [Service]
# Environment=CLONOTH_QQ_CACHE_MAX_AGE_SECONDS=259200
# Environment=CLONOTH_QQ_CACHE_ROOTS=/srv/qq-extra
```

Machines set up before this timer existed get it by re-running `install_provision.sh` if they run several instances. A single-instance machine installs it by hand, as root, right after updating with `deploy.sh` so the copy matches what was just pulled:

```bash
install -d -m 0755 -o root -g root /usr/local/lib/kazebot
install -m 0644 -o root -g root /opt/kazebot/adapters/onebot/qq_cache.py /usr/local/lib/kazebot/qq_cache.py
install -m 0644 -o root -g root /opt/kazebot/deploy/systemd/kazebot-host-cleanup.service \
  /opt/kazebot/deploy/systemd/kazebot-host-cleanup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now kazebot-host-cleanup.timer
```

The main instance's own cleanup keeps running the task until `kazebot.service` carries `CLONOTH_MAINTENANCE_SCOPE=workspace` (`systemctl edit kazebot.service`, then restart it). That run is harmless, since it cannot delete the containers' files, but its log keeps reporting `deleted=0`.

Dry run, schedule and logs:

```bash
sudo /usr/bin/python3 -I /usr/local/lib/kazebot/qq_cache.py --dry-run
systemctl list-timers kazebot-host-cleanup.timer
journalctl -u kazebot-host-cleanup.service -n 100 --no-pager
```

## Data cleanup timer

`clonoth-data-cleanup.timer` runs `engine.data_cleanup` periodically. The cleanup script rotates large event logs and removes expired temporary files, artifacts, child-session files, node-context files, and files under `data/attachments/`.

Default retention is defined in `engine/data_cleanup.py`:

- `data/events.jsonl`: rotate at 50 MiB and keep 3 backups
- `data/signals.jsonl`: rotate at 20 MiB and keep 2 backups
- `data/attachments/`: 24 hours
- temporary/artifact files: 24 hours
- child sessions: 24 hours
- node contexts: 48 hours
- QQ/NapCat internal cache: 7 days by default
- persistent memory entries: 14 days by default for non-constant entries

Memory cleanup scans `data/memory/**/*.yaml` and removes stale entries whose `constant` field is not true. Staleness is based on `last_hit_at` first, then `updated_at`, then `created_at`; entries without timestamps are kept for safety.

Signal file logging can be reduced independently of the in-process signal bus. The default `config/runtime.yaml` excludes `stream_delta`, `tool_call_delta`, and the large duplicate `tool_call_end` payload from `signals.jsonl`; `events.jsonl` and lightweight completion signals still retain the corresponding audit/monitoring information. Configure `engine.signals.bridge_exclude_patterns` to change this denylist.

The QQ/NapCat cleanup is conservative. It does not wipe the whole QQ data directory; it only removes stale image/video media files or files under cache-like directories. This explicitly includes NTQQ account media cache folders such as `<qq号>/Image` and `<qq号>/Video`, whose files may not always have standard media extensions. It can also discover QQ cache paths through `/proc/<qq-pid>/root/...`, which is needed when QQ runs with `HOME=/app` or `--user-data-dir=/app/.config/QQ` in a separate mount namespace.

Optional environment overrides in `/opt/Clonoth/.env`:

```bash
# Disable QQ/NapCat internal cache cleanup:
# CLONOTH_QQ_CACHE_MAX_AGE_SECONDS=0

# Default is 7 days:
CLONOTH_QQ_CACHE_MAX_AGE_SECONDS=604800

# Optional extra comma-separated roots. Auto-discovered roots are still used.
# CLONOTH_QQ_CACHE_ROOTS=/app/.config/QQ,/app/napcat,/root/.config/QQ

# Disable memory entry cleanup:
# CLONOTH_MEMORY_ENTRY_MAX_AGE_SECONDS=0

# Default is 14 days:
CLONOTH_MEMORY_ENTRY_MAX_AGE_SECONDS=1209600
```

### Install / update

```bash
cd /opt/Clonoth
sudo cp deploy/systemd/clonoth-data-cleanup.service /etc/systemd/system/
sudo cp deploy/systemd/clonoth-data-cleanup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now clonoth-data-cleanup.timer
```

### Check status

```bash
systemctl list-timers --all | grep clonoth-data-cleanup
systemctl status clonoth-data-cleanup.timer
journalctl -u clonoth-data-cleanup.service -n 100 --no-pager
tail -n 100 /opt/Clonoth/data/logs/cleanup.log
```

### Run once manually

```bash
sudo systemctl start clonoth-data-cleanup.service
```
