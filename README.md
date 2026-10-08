# Jeeves

Read the [Vox Privacy Policy](PRIVACY.md) for data processing details and privacy requests.
Contact [wabbit@wabbit.one](mailto:wabbit@wabbit.one) with privacy questions or deletion requests.

The runtime uses SQLCipher 4 for all persistent Vox databases. Set
`JEEVES_DB_KEY_FILE` to a protected key file before starting it, and follow the
[SQLCipher migration guide](deploy/SQLCIPHER.md) for existing SQLite databases.
The live service was migrated on October 8, 2026, with original databases and verified
local snapshots retained for rollback.

## Discord invocation and intent review

Vox responds to actual Discord `@Vox` mentions, direct messages, replies to its messages,
and standalone `v` or `vox` names (case-insensitive). Custom personality names and initials
also remain supported. Bare names in ordinary server messages require Discord's
Message Content intent. Actual mentions and DMs can provide content without that intent;
server replies need "ping on reply" enabled for Discord to provide their content.

In the YAML config selected by `JEEVES_CONFIG_PATH` (or `.private.yml`), use:

```yaml
discord:
  token: "<your-discord-token>"
  message_content_intent: true
  members_intent: false
```

Message Content defaults to `true` to preserve name triggers and channel context.
It must also be enabled and, when required, approved in the Developer Portal.
Guild Members defaults to `false`; enable it only for approved roster/event functionality.
Presence is never requested. To run without any privileged intents, set both flags to
`false` and restart Vox.

If Discord rejects privileged intents with Gateway code `4014`, Vox closes the rejected
session and reconnects once without them. Actual mentions and DMs remain available;
bare name triggers in ordinary server messages and passive history indexing do not.
Roster polling is skipped without Guild Members access. The fallback lasts until a
restart; restore access in the Developer Portal before restarting with the flags enabled.

The [intent review draft](docs/discord-intent-review-draft.md) contains the form answers
and evidence requirements. Add the public [Vox Privacy Policy](PRIVACY.md) URL to
the Developer Portal before submission.

Vox does not use Discord message content to train or fine-tune models. This
repository contains the bot runtime and its operational tools. Topic notifications
use a pretrained embedding model to compare messages with requested topics.

## Channel controls and privacy commands

The runtime enables existing and new channels and threads by default,
subject to Discord permissions. Moderators can disable processing in individual
channels; those choices persist across restarts. DMs sent directly to Vox remain
available. These changes were deployed on October 8, 2026.

- `/vox privacy` opens a private embed with the policy, privacy contact, `/vox optout`,
  and instructions to request access or deletion. It remains available after opt-out.
- `/vox channel enable [channel]` resumes indexing, contextual replies, and requested
  features in a disabled channel, including accessible history. It replies privately.
  It does not require or post a public announcement.
- `/vox channel status [channel]` privately shows the processing status.
- `/vox channel disable [channel]` privately asks the invoking moderator to confirm
  stopping processing and deleting that channel's active archive, feature records,
  notification history, and configured voice transcripts. Cleanup is retried after
  reconnecting if interrupted. Historical logs, exports, recovery copies, and
  provider copies still require operator cleanup through `wabbit@wabbit.one`.

The optional channel defaults to the current channel. Controls require current
Administrator, Manage Server, Manage Channels, or Manage Messages permission in
that channel, checked again at confirmation. Configured global administrators
receive no override. Disabled channels retain only their control identifiers;
other channels and global opt-out records survive cleanup. A moderator may later
enable collection again, including accessible history. Channel controls affect the
selected channel; threads have their own controls.

Live history search, local indexed search, and requested search indexing check
current Discord membership, View Channel, and Read Message History permissions.
Private threads also require current membership or Manage Threads. Retrieval
stays within the request channel and server so a private excerpt cannot be posted
to a broader audience. DMs can retrieve only the requester's conversation with Vox.
Topic notifications recheck the recipient's current access to the source channel.

## Server removal and retention

The runtime deletes a server's active indexed messages, metadata,
conversation records, reminders, topic/feed subscriptions, notification history,
and configured voice transcripts when Vox leaves that server. It also clears cached
context and cancels processing. Checks after READY catch removals while offline;
failed cleanup is retried. Temporarily unavailable servers retain their records.
Other servers, DMs, and global opt-outs are preserved. The Discord client's duplicate
message cache is disabled so Vox owns the cached context deletion lifecycle.

Identifier-only `retention_removed_guilds` and `retention_removed_channels` tables
block delayed writes and journal unfinished cleanup. Reinstallation permits fresh
collection after cleanup completes. Preserve these markers and opt-out records
when restoring state. Commitments now record `guild_id`; legacy rows can still be
deleted through indexed or cached channel ownership. Legacy reminders whose server
cannot be determined require operator review before deployment.

Historical service logs, exports, recovery copies, and provider-held data require
separate operator deletion. The deployed changes remove response content and tool
arguments/results from normal bot logs and keep new voice transcripts in per-server
folders; existing mixed transcript files are filtered during removal. The public
policy describes the deployed safeguards and the treatment of retained older copies.

## Privacy opt-out

Run `/vox optout` in a server or DM to open an ephemeral confirmation visible only to
you. **Confirm opt-out** permanently disables your Vox features across all servers
and DMs; **Cancel** leaves your settings unchanged. The confirmation expires after
three minutes. A text request such as `vox I would like to opt-out of processing of
my data` directs you to the slash command and is handled without a model request.

After confirmation, Vox ignores your authored messages and voice, including messages
fetched later through history or search. It removes your authored messages from the
active index, clears in-memory conversation context, cancels in-flight work, removes
your reminders and topic subscriptions, and blocks further feature use. Minimal
user and message identifiers remain to enforce the preference and prevent partial
edits from restoring withdrawn messages. These records live in the existing index
database and must be preserved during deployment or restore. Feature databases also
retain the blocked user IDs to prevent stale workers from recreating records.

The slash command works without Message Content access. `/vox optout` remains available
to show your opt-out status, but there is no self-service opt-in command. If saving
fails, processing stays paused and the command lets you retry; startup completes
feature cleanup for persisted opt-outs before accepting messages. The preference
does not delete messages from Discord or erase old logs, backups, or copies already
sent to providers. Operators must handle those separately when fulfilling deletion
requests. No command is registered until the prepared version is started; startup
upserts `/vox` individually and preserves unrelated application commands.

## LLM Throttling

Interactive Vox replies use a global SQLite-backed throttle policy stored in the same index DB as the background indexer.

By default, the bot seeds this singleton policy row:

```sql
policy_id = 1
enabled = 0
window_seconds = 3600
max_requests = 5
downgraded_reasoning_effort = 'low'
```

That means throttling is off by default, but the default policy shape is "more than 5 LLM-triggering requests from the same user in 1 hour drops `reasoning_effort` to `low`".

The tables are:

```sql
llm_throttle_policy
llm_request_events
llm_high_reasoning_exempt_roles
```

`llm_request_events` only counts messages that would actually invoke the LLM. Ordinary Discord chatter is not part of the throttle budget.

Exemptions:

- Global admin user IDs from `.private.yml` via `admin_user_ids`
- Discord users with the `Administrator` permission in the guild
- Guild roles listed in `llm_high_reasoning_exempt_roles`

If you are using the default index DB path, configure it with:

```bash
sqlite3 servant_index.sqlite3 <<'SQL'
UPDATE llm_throttle_policy
SET enabled = 1,
    window_seconds = 3600,
    max_requests = 5,
    downgraded_reasoning_effort = 'low',
    updated_at = CAST(unixepoch('now') * 1000 AS INTEGER)
WHERE policy_id = 1;

INSERT OR IGNORE INTO llm_high_reasoning_exempt_roles (guild_id, role_id, created_at)
VALUES ('<guild_id>', '<role_id>', CAST(unixepoch('now') * 1000 AS INTEGER));
SQL
```

If you override `indexer_db_path` in `.private.yml`, point `sqlite3` at that file instead.
