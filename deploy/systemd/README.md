# Clonoth systemd units

This directory contains optional systemd unit templates for a Debian deployment under `/opt/Clonoth`.

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
