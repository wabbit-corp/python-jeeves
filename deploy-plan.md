# Deployment Plan: `python-jeeves` on `discord-bots`

This document is a deployment review and first-pass execution plan for moving `python-jeeves` onto the existing shared DigitalOcean host:

- Hostname: `discord-bots`
- Public IP: `204.48.31.156`
- Admin login: `deploy@204.48.31.156`
- Current host size: `s-1vcpu-1gb`
- Current co-tenant service: `the-dark-knight.service`

The main goal is to deploy Jeeves without losing the current SQLite state.

## Executive Summary

The project is deployable to the same host, but the first deployment should use `systemd`, not the current Docker setup.

Why:

- `servant_app.py` loads `.private.yml` from the working directory and defaults all SQLite files to the working directory unless explicit absolute paths are configured.
- The current `docker-compose.yml` only mounts `.private.yml` and the CODI model directory. It does not persist any of the SQLite databases, so it is not safe for a real migration.
- The host has enough disk, but limited RAM. As of `2026-03-29`, it had about `570 MiB` available and `0 B` swap. Base bot runtime should fit, but heavy features like sentence-transformers and CODI work need care.

Recommended first deployment model:

- deploy code under `/opt/python-jeeves/current`
- keep secrets in `/etc/python-jeeves/private.yml`
- keep all writable state under `/var/lib/python-jeeves/`
- run as a dedicated `python-jeeves` service user
- manage with a dedicated `systemd` unit, probably `discord-bot-jeeves.service`

## Findings From The Repo Review

### 1. Runtime entrypoint and config model

- The app starts from `servant_app.py`.
- It loads config from `.private.yml` in the current working directory.
- Nested YAML keys are flattened into `ctx.config`.
- Secret/config loading is file-based, not env-based.
- The bot initializes the background index DB on startup and then connects to Discord.

Operational consequence:

- the deployed working directory matters
- `.private.yml` must exist at runtime in that working directory, or be symlinked there
- if we want the DBs somewhere safer than the repo checkout, we must set absolute DB paths in `.private.yml`

### 2. Persistent SQLite state that must be preserved

Current repo-root live-looking database files:

- `servant_index.sqlite3` (`162M`)
- `servant_commitments.sqlite3` (`28K`)
- `servant_event_channels.sqlite3` (`32K`)
- `servant_topic_subscriptions.sqlite3` (`96K`)

Current repo-root backup or ambiguous copies that should not be treated as the active deployment state unless explicitly chosen:

- `servant_index_2.sqlite3` (`152M`)
- `servant_index_backup.sqlite3` (`143M`)
- `servant_commitments copy.sqlite3` (`20K`)

Role of the main DBs:

- `servant_index.sqlite3`
  - background Discord index
  - indexed search data
  - LLM throttle tables
  - permission/guild lookup data
  - CODI conversation tables when used
- `servant_commitments.sqlite3`
  - commitment/check-in workflow state
- `servant_event_channels.sqlite3`
  - ICS / YouTube subscription state
- `servant_topic_subscriptions.sqlite3`
  - semantic topic subscription state

Important implementation detail:

- multiple modules enable SQLite WAL mode
- preserving only the base `.sqlite3` file is not always enough if a writer is active
- the migration should use SQLite logical backups or a clean stop before copying

### 3. Other state worth preserving

- `codi/api/training/tmp/models/` is currently about `661M`
- CODI tools look for `model.pickle`
- `codi_model_dir` can be set in config
- underlying CODI code also honors `CODI_MODEL_DIR`

Recommendation:

- preserve the current CODI model directory in the first move, even if the main bot runtime does not need it immediately

### 4. Current deployment asset gap

There are no Jeeves-specific deploy assets in this repo yet for the shared host.

Current gaps:

- no `systemd` unit in this repo
- no host deployment notes
- no persistent-data directory convention
- no safe DB migration helper
- no documented cutover procedure

### 5. Docker is not ready for production migration here

The current `docker-compose.yml` is insufficient for a real deployment because it mounts:

- `./.private.yml:/app/.private.yml:ro`
- `codi-models:/app/codi/api/training/tmp/models`

It does not mount:

- `servant_index.sqlite3`
- `servant_commitments.sqlite3`
- `servant_event_channels.sqlite3`
- `servant_topic_subscriptions.sqlite3`
- any sentence-transformers / Hugging Face cache

If used as-is, container replacement would risk data loss or state fragmentation.

### 6. Shared-host resource fit

Live host check on `2026-03-29`:

- memory: `961 MiB` total, `570 MiB` available
- swap: none
- disk: `24G` total, `22G` free
- current Dark Knight process RSS: about `44 MiB`

Conclusion:

- disk is not the constraint
- RAM is the real constraint
- a basic Jeeves runtime may fit
- enabling heavier features may need swap and monitoring

Specific risk areas:

- `sentence-transformers` in topic subscriptions may download/load model data at runtime
- CODI assets are not small
- `crawl4ai` may need extra browser/runtime validation on the server

### 7. Embedding backend probe results

Two materially different Linux behaviors showed up during probing on `2026-03-29`.

#### Default `sentence-transformers` / `torch` path

A throwaway install of `sentence-transformers==5.2.0` on Ubuntu pulled a very large Linux `torch` stack, including CUDA-related packages, during dependency resolution.

Operational consequence:

- the default Linux packaging story is much heavier than the local macOS dev environment suggests
- this is a poor fit for a `1G` shared host unless we explicitly pin a CPU-only torch stack

#### `fastembed` ONNX path

A throwaway `fastembed` probe on the live host successfully loaded and ran the exact model:

- `sentence-transformers/all-MiniLM-L6-v2`

Measured under a bounded transient unit with:

- `MemoryHigh=250M`
- `MemoryMax=350M`
- low CPU weight / nice priority

Observed results:

- after import: about `81.5 MiB` RSS
- after model construction/download: about `222.8 MiB` RSS
- after first embed: about `226.7 MiB` RSS
- high-water mark during the run: about `239.7 MiB`
- embedding dimension: `384`
- host `MemAvailable` dropped from about `586.9 MiB` to about `393.9 MiB` at model load, then stabilized around `394 MiB`
- probe completed successfully in under `3s`

On-disk footprint after the probe:

- `fastembed` venv: about `304M`
- model cache: about `87M`

Conclusion:

- an ONNX-backed path is viable on this host
- if topic subscriptions stay enabled on `discord-bots`, `fastembed` is a much better fit than the default Linux `sentence-transformers` install path
- if we do keep `sentence-transformers`, we should treat CPU-only torch pinning as mandatory deployment work, not an optimization

## Recommended Target Layout On The Host

Use a dedicated service user and standardize writable state away from the checkout.

### Naming

- service user: `python-jeeves`
- app directory: `/opt/python-jeeves/current`
- config directory: `/etc/python-jeeves`
- state directory: `/var/lib/python-jeeves`
- cache directory: `/var/cache/python-jeeves`
- service unit: `discord-bot-jeeves.service`

### Filesystem layout

Code and venv:

- `/opt/python-jeeves/current`
- `/opt/python-jeeves/current/.venv`

Config:

- `/etc/python-jeeves/private.yml`
- `/opt/python-jeeves/current/.private.yml` -> symlink to `/etc/python-jeeves/private.yml`

Persistent state:

- `/var/lib/python-jeeves/sqlite/servant_index.sqlite3`
- `/var/lib/python-jeeves/sqlite/servant_commitments.sqlite3`
- `/var/lib/python-jeeves/sqlite/servant_event_channels.sqlite3`
- `/var/lib/python-jeeves/sqlite/servant_topic_subscriptions.sqlite3`
- `/var/lib/python-jeeves/codi-models/model.pickle`

Caches:

- `/var/cache/python-jeeves/huggingface`

Logging:

- journald via `systemd`
- no app-local log files by default

## Recommended Config Changes For Deployment

Do not keep deployment DBs in the repo checkout. Point them at absolute host paths.

The deployed `/etc/python-jeeves/private.yml` should explicitly define at least:

```yaml
openai:
  key: "<secret>"
discord:
  token: "<secret>"

indexer_db_path: /var/lib/python-jeeves/sqlite/servant_index.sqlite3
commitments_db_path: /var/lib/python-jeeves/sqlite/servant_commitments.sqlite3
event_channels_db_path: /var/lib/python-jeeves/sqlite/servant_event_channels.sqlite3
topic_subscriptions_db_path: /var/lib/python-jeeves/sqlite/servant_topic_subscriptions.sqlite3
codi_model_dir: /var/lib/python-jeeves/codi-models
admin_user_ids:
  - "<discord-user-id>"
```

Additional recommended runtime environment in the service unit:

- `HF_HOME=/var/cache/python-jeeves/huggingface`
- `CODI_MODEL_DIR=/var/lib/python-jeeves/codi-models`

Those do not replace the YAML config, but they make model/cache behavior more explicit.

## Recommended Deployment Strategy

### Phase 0: Host preparation

Before deploying Jeeves:

1. Add swap to the host.
2. Create the `python-jeeves` service user.
3. Create `/opt/python-jeeves`, `/etc/python-jeeves`, `/var/lib/python-jeeves/sqlite`, `/var/lib/python-jeeves/codi-models`, and `/var/cache/python-jeeves/huggingface`.
4. Install only the host packages actually needed for runtime.

Suggested baseline packages:

- `python3-venv`
- `build-essential`
- `gcc`
- `g++`
- `libgomp1`
- `sqlite3`

Swap recommendation:

- add at least `1G`, preferably `2G`, before placing a second bot on a `1G` machine

### Phase 1: Repo preparation

Before touching the host, add deployment artifacts to this repo:

1. `deploy/discord-bot-jeeves.service`
2. deployment README or server notes
3. `.private.yml` deployment template with explicit absolute DB paths
4. optional helper for safe SQLite snapshotting

Nice-to-have code improvements before deployment:

1. support a config path override like `JEEVES_CONFIG_PATH` so runtime is not coupled to `.private.yml` in the working directory
2. make persistent-path configuration more centralized and documented
3. add deployment docs for `systemd`

These are not strictly required for the first move because the symlink approach works.

### Phase 2: Select and normalize the data to migrate

Treat the following as authoritative unless a later manual review says otherwise:

- `servant_index.sqlite3`
- `servant_commitments.sqlite3`
- `servant_event_channels.sqlite3`
- `servant_topic_subscriptions.sqlite3`
- `codi/api/training/tmp/models/`

Do not deploy these ambiguous backup copies into the live state directory:

- `servant_index_2.sqlite3`
- `servant_index_backup.sqlite3`
- `servant_commitments copy.sqlite3`

Instead:

1. archive them elsewhere if you want to keep them
2. keep the production state directory limited to one canonical DB per subsystem

### Phase 3: Create consistent SQLite snapshots

Preferred approach:

1. stop any local Jeeves writer if one is running
2. create SQLite backup snapshots with `sqlite3 ... ".backup ..."` for each active DB
3. validate each snapshot with `PRAGMA integrity_check;`
4. transfer the snapshots to the host

Why this instead of a raw file copy:

- the code uses WAL mode
- `.backup` is less error-prone than hoping no `-wal` file is active

If the local bot is definitely not running and there are no `-wal` files, raw copy is probably fine, but `.backup` is still the safer cutover method.

### Phase 4: Deploy the code

Deploy the application checkout to:

- `/opt/python-jeeves/current`

Do not deploy:

- local `.venv`
- repo-root SQLite files
- coverage/test caches
- `.git`
- backup DB copies

On the host:

1. create a fresh venv
2. install dependencies into that venv
3. install the config file under `/etc/python-jeeves/private.yml`
4. symlink `/opt/python-jeeves/current/.private.yml` to `/etc/python-jeeves/private.yml`
5. place the SQLite snapshots in `/var/lib/python-jeeves/sqlite/`
6. place CODI models in `/var/lib/python-jeeves/codi-models/`
7. chown writable dirs to `python-jeeves:python-jeeves`

### Phase 5: Install and start the service

Install a unit roughly shaped like:

- `User=python-jeeves`
- `Group=python-jeeves`
- `WorkingDirectory=/opt/python-jeeves/current`
- `ExecStart=/opt/python-jeeves/current/.venv/bin/python /opt/python-jeeves/current/servant_app.py`
- `Restart=always`
- `NoNewPrivileges=yes`

Recommended extra unit settings:

- `Environment=HF_HOME=/var/cache/python-jeeves/huggingface`
- `Environment=CODI_MODEL_DIR=/var/lib/python-jeeves/codi-models`

Then:

1. `daemon-reload`
2. `enable --now`
3. follow `journalctl -u discord-bot-jeeves.service -f`

### Phase 6: Smoke-test the first boot

Minimum first-boot checks:

1. service stays up for several minutes
2. Discord login succeeds
3. `servant_index.sqlite3` is readable and writable by the service
4. the service can still see existing commitment/event/topic data
5. no permission failures writing WAL/shm files

Recommended explicit checks:

1. `sudo systemctl status discord-bot-jeeves.service --no-pager`
2. `sudo journalctl -u discord-bot-jeeves.service -n 200 --no-pager`
3. `sudo -u python-jeeves sqlite3 /var/lib/python-jeeves/sqlite/servant_index.sqlite3 'PRAGMA integrity_check;'`
4. compare row counts between local and server snapshots for key tables

Suggested key-table spot checks:

- `messages`
- `channels`
- `guilds`
- `commitments`
- `event_channel_subscriptions`
- `topic_subscriptions`

### Phase 7: Watch live resource usage

After first boot, watch:

- `free -h`
- `ps -eo pid,user,%mem,%cpu,rss,args --sort=-rss | head`
- `journalctl`

If memory gets tight:

1. keep swap enabled
2. avoid CODI training work on this host
3. consider disabling or deferring topic-subscription usage until model-cache behavior is confirmed
4. consider moving Jeeves to a larger host if heavy features are truly needed at runtime

## Recommended Order Of Work

This is the safest sequence:

1. add deploy assets and documentation to the repo
2. run the full local check suite
3. prepare authoritative DB snapshots
4. add swap and host directories
5. upload code, config, models, and DB snapshots
6. install and start the service
7. validate DB integrity and Discord connectivity
8. monitor memory for at least one real usage window

## Rollback Plan

If the first deployment misbehaves:

1. stop `discord-bot-jeeves.service`
2. keep `the-dark-knight.service` untouched
3. restore the last known-good DB snapshots
4. restore the previous app checkout if code caused the issue
5. restart only after integrity and config checks pass

Because this is an additive deployment on a shared host, rollback is straightforward as long as the Jeeves state lives in its own directories.

## Follow-Up Work After The First Successful Deployment

After the first stable deploy, clean up the deployment story:

1. add formal Jeeves server docs under `../deployed/`
2. document the new service in the shared host notes
3. consider adding a real backup procedure for `/var/lib/python-jeeves/sqlite`
4. consider refactoring config loading away from hardcoded `.private.yml`
5. only revisit Docker after persistent mounts and runtime requirements are fully specified

## Bottom Line

Deploying Jeeves onto `discord-bots` is reasonable if we:

- preserve the SQLite state explicitly
- use `systemd` first
- move writable data out of the checkout
- add swap before cutover
- treat heavier ML/browser features as things to validate, not things to assume

What should not happen:

- reusing the current `docker-compose.yml` unchanged
- leaving production SQLite files in the checkout directory
- copying ambiguous backup DBs into the live state path
- deploying onto the current `1G` host without at least some swap
