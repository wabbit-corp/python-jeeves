# SQLCipher deployment and migration

The Vox runtime requires SQLCipher 4 through `sqlcipher3>=0.6.2,<0.7.0`.
Every connection to the message index, commitments, event channels, and topic
subscriptions uses a 256-bit key from the file selected by `JEEVES_DB_KEY_FILE`.
The service template selects `/etc/python-jeeves/database.key`. There is no
plaintext fallback: a missing key, incorrect key, or unmigrated database prevents
that connection from opening. Startup checks the key and opens the index before
accepting Discord messages.

Production was migrated on October 8, 2026. The active release is
`/opt/python-jeeves/releases/2026-10-08-privacy-v1`; all four databases were exported
without changing their originals, with integrity and per-table row counts checked
before cutover. Production plaintext originals and historical logs were later
removed after all 93 retired files were verified in a complete local archive.
Server recovery archives now use age encryption; local recovery copies are on the
operator's FileVault-encrypted workstation. See the local deployment
record in `/Users/wabbit/ws/datatron/vox-deployment-backups/2026-10-08/README.md`.
The active service allows up to 256 MiB of locked memory for SQLCipher's protected
allocations (`LimitMEMLOCK=256M`).

## Protect the key

Generate a new random 32-byte key as 64 hexadecimal characters, stored in a
regular file with mode `0600` or `0640`. On the deployment host, an administrator
can create it without printing the key or passing it in process arguments:

```bash
sudo python3 - <<'PY'
import grp
import os
import secrets

path = "/etc/python-jeeves/database.key"
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
with os.fdopen(fd, "w", encoding="ascii") as handle:
    os.fchmod(handle.fileno(), 0o640)
    os.fchown(handle.fileno(), 0, grp.getgrnam("python-jeeves").gr_gid)
    handle.write(secrets.token_hex(32) + "\n")
    handle.flush()
    os.fsync(handle.fileno())
PY
```

Keep a protected recovery copy of the key separately from database snapshots.
Losing the key makes encrypted records unrecoverable. Do not commit keys, include
them in repository syncs, print them in logs, or replace the key file while Vox is
running. This migration does not implement key rotation.

## Migrate the existing databases offline

Install the prepared runtime and its requirements before cutting over. Stop
`discord-bot-jeeves.service` and any other processes that open these databases.
Keep it stopped until all four databases have been replaced and verified.

For each existing database, create a separate encrypted copy:

```bash
cd /opt/python-jeeves/current
sudo -u python-jeeves env JEEVES_DB_KEY_FILE=/etc/python-jeeves/database.key \
  .venv/bin/python -m servant.scripts.encrypt_databases \
  /var/lib/python-jeeves/sqlite/servant_index.sqlite3 \
  /var/lib/python-jeeves/sqlite/servant_index.sqlite3.encrypted
```

Repeat for `servant_commitments.sqlite3`, `servant_event_channels.sqlite3`, and
`servant_topic_subscriptions.sqlite3` when they exist. Missing feature databases
will be created encrypted on first use. The tool reads a consistent source
snapshot, including committed WAL records, exports schema and data, preserves
`user_version` and `application_id`, and verifies both database integrity and
page authentication. It refuses to overwrite an existing destination and leaves
the plaintext source unchanged, including on failure.

After all exports succeed, make protected encrypted recovery copies. While all
writers remain stopped, replace the original database filenames with their
verified encrypted copies. Remove each original database's `-wal`, `-shm`, and
`-journal` files before opening its encrypted replacement: plaintext sidecars
must never be reused with an encrypted database. Install the updated service
unit, run `systemctl daemon-reload`, then start Vox. Verify normal replies,
history search, reminders, subscriptions, and a test account's opt-out.

Audit and remove or encrypt older plaintext database copies, deployment staging
directories, exports, and backups. File deletion alone cannot guarantee removal
of old blocks from an SSD or provider snapshots. Do not claim that historical
data is encrypted merely because the active filenames have been replaced.

## Encrypted snapshots and remaining storage

`deploy/stage-state.sh` now uses SQLCipher export for encrypted snapshots. Set
`JEEVES_DB_KEY_FILE`; set `JEEVES_PYTHON` when the runtime Python is not the repo's
`.venv/bin/python`. The helper rejects plaintext sources rather than creating
plaintext backups. Restore with the same protected key and preserve opt-out
identifier tables and the latest `retention_removed_guilds` and
`retention_removed_channels` deletion markers. Reconcile installed servers before
allowing processing after restore; a historical snapshot must not overwrite newer
opt-out or deletion records.

SQLCipher protects database pages and WAL/journal contents. Temporary SQL data
stays in memory. It does not encrypt application logs, configured voice
transcript files, downloaded attachments, or administrative CSV/ZIP exports.
Those records need separate encrypted storage or removal of sensitive content
before an operator certifies that all retained Discord data is encrypted at rest.
Update the public privacy policy only after verifying the deployed storage and
the treatment of historical copies.

The October 8 production service uses a bounded volatile journald namespace and
isolated tmpfs mounts for temporary exports; persistent voice transcript files
are disabled. Host swap uses dm-crypt AES-XTS with a fresh random key per
activation, so memory storage cannot fall back to a plaintext swap file. These
host settings are part of the storage guarantee and must be preserved on future
deployments. The generated swap and cryptsetup units were activated and checked
again after a full host restart. Vox reconnected to Discord, all four databases
passed integrity/page-authentication checks, and the encrypted swap and memory
storage settings were active after boot. The host configuration is recorded in the
private deployment record and `data-infrastructure/discord-bots.md`.
