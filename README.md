# Jeeves

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
