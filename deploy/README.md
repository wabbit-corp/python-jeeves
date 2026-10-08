# Deployment Assets

This directory contains the local assets needed to deploy `python-jeeves` onto the shared `discord-bots` host. The October 8, 2026 release is deployed; see [SQLCipher deployment and migration](SQLCIPHER.md) for its storage and recovery status.

## Files

- `discord-bot-jeeves.service`
  - `systemd` unit for the host
- `journald@vox.conf`
  - bounded in-memory diagnostic journal for the Vox service
- `private.yml.production.tmpl`
  - production config template for `/etc/python-jeeves/private.yml`
- `install-runtime.sh`
  - host-side runtime install/update helper
- `stage-state.sh`
  - local helper that creates verified SQLCipher snapshots in a staging directory
- `SQLCIPHER.md`
  - protected key provisioning, offline migration, encrypted snapshots, and rollout checks
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

1. Stage the current encrypted database state:

Follow [SQLCipher deployment and migration](SQLCIPHER.md) first. The runtime requires encrypted databases and `JEEVES_DB_KEY_FILE`; the staging helper
also requires that key and refuses plaintext sources.

```bash
JEEVES_DB_KEY_FILE=/path/to/protected/database.key ./deploy/stage-state.sh
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

Install `journald@vox.conf` as `/etc/systemd/journald@vox.conf` before starting
the service. The unit uses `LogNamespace=vox` so diagnostic output remains in a
bounded volatile journal. Read it with `journalctl --namespace=vox -u
discord-bot-jeeves`. Its isolated `/tmp` and `/var/tmp` mounts use tmpfs for
administrative exports and other temporary files. `PrivateTmp=no` is intentional:
`PrivateTmp=yes` can place disk-backed bind mounts over the tmpfs mounts. Verify
the running process's mount information after deployment. Persistent voice
transcripts are disabled in the production configuration.

## Runtime Requirements

Use `requirements.runtime.txt` for the host runtime install.

Why:

- topic subscriptions now use `fastembed`
- the runtime loads a pretrained ONNX model for topic matching
- no local model-fitting tools are installed or shipped with the bot
