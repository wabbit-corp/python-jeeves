# Deployment Assets

This directory contains the local assets needed to deploy `python-jeeves` onto the shared `discord-bots` host without performing the deployment itself.

## Files

- `discord-bot-jeeves.service`
  - `systemd` unit for the host
- `private.yml.production.tmpl`
  - production config template for `/etc/python-jeeves/private.yml`
- `install-runtime.sh`
  - host-side runtime install/update helper
- `stage-state.sh`
  - local helper that creates clean SQLite snapshots and copies CODI models into a staging directory
- `rsync-excludes.txt`
  - excludes for syncing the repo to the host

## Runtime Assumptions

- app checkout lives at `/opt/python-jeeves/current`
- config file lives at `/etc/python-jeeves/private.yml`
- service user is `python-jeeves`
- writable state lives under `/var/lib/python-jeeves`
- caches live under `/var/cache/python-jeeves`

The service unit uses `JEEVES_CONFIG_PATH=/etc/python-jeeves/private.yml`, so the deployment no longer depends on a `.private.yml` symlink in the working directory.

## Recommended Preparation Workflow

1. Stage the current SQLite state and CODI models:

```bash
./deploy/stage-state.sh
```

2. Copy `deploy/private.yml.production.tmpl` to a real production config and fill in secrets and admin IDs.

3. Sync the repo to the host with the provided excludes:

```bash
rsync -az --delete --exclude-from=deploy/rsync-excludes.txt \
  ./ deploy@<host>:/opt/python-jeeves/current/
```

4. On the host, install the runtime venv from the lean runtime requirements:

```bash
cd /opt/python-jeeves/current
./deploy/install-runtime.sh
```

5. Install the `systemd` unit and enable it only when ready.

## Runtime Requirements

Use `requirements.runtime.txt` for the host runtime install.

Why:

- topic subscriptions now use `fastembed`
- the runtime no longer needs `sentence-transformers`
- keeping `sentence-transformers` off the host avoids the large Linux `torch` dependency path discovered during probing

Local training / annotation tooling can keep using the full local development environment.
