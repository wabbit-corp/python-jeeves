# Conversations and CODI Data Workflow

This doc describes how to build the message index, run CODI conversation indexing, and export training data.
All steps can be run offline once you have a `servant_index.sqlite3` snapshot.

## Prereqs
- Use the repo `.venv` for all commands.
- `servant_index.sqlite3` lives in the repo root by default.
- `codi/api/training/tmp/models/model.pickle` is the default CODI model location.
- `.private.yml` can provide `discord.token` (for indexing) and `openai.key` (for naming/annotation).

## 1) Build or refresh the message index
The background indexer runs inside the bot and writes to `servant_index.sqlite3`.

```
.venv/bin/python servant_app.py
```

Notes:
- The DB is created in the current working directory as `servant_index.sqlite3`.
- If you already have a DB snapshot, you can skip this step and pass `--db-path` to the scripts below.

## 2) Run CODI conversation indexing (writes codi_* tables)
For a single channel:

```
.venv/bin/python servant/scripts/codi_run_channel.py \
  --channel-id 1234567890 \
  --db-path /path/to/servant_index.sqlite3 \
  --model-dir codi/api/training/tmp/models
```

Notes:
- This writes `codi_channel_state`, `codi_conversations`, and `codi_conversation_messages` into the same DB.
- `--features` supports `chat`, `discourse`, `content`, or `all`.
- If `openai.key` is set, conversation naming runs; otherwise IDs and hashes still persist.
- Use `--output` to save a JSON summary (or omit it to print to stdout).

## 3) Export CODI training JSON
Once the CODI tables exist, export training payloads per channel:

```
.venv/bin/python servant/scripts/export_codi_training.py \
  --db-path /path/to/servant_index.sqlite3 \
  --out-dir exports/codi \
  --channel-id 1234567890 \
  --require-conversations
```

Options:
- Use `--guild-id` (repeatable) to export all channels in a guild.
- Omit `--require-conversations` to export message data without conversation labels.

Outputs are written to `exports/codi/channel_<id>.json`.

## 4) Optional: auto-annotate exported JSON
Auto-annotation uses OpenAI to label conversations in exported JSON:

```
.venv/bin/python servant/scripts/codi_auto_annotate.py \
  --input exports/codi/channel_1234567890.json \
  --out-dir exports/codi/annotated \
  --openai-key "$OPENAI_API_KEY"
```

This produces `*.annotated.json` alongside the input (or in `--out-dir`).
