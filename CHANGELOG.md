# Changelog

All notable changes to the Servant project will be documented in this file.

The entries below are derived from the git history and grouped by release date.

## [Unreleased]
### Added

- `/vox privacy` provides private access to the policy, privacy contact, opt-out command, and deletion instructions. Moderator-only `/vox channel enable`, `status`, and privately confirmed `disable` provide durable stop-and-clear cleanup and restart recovery. Existing and new channels and threads are enabled by default subject to Discord permissions; moderators' disable choices persist, and channel controls reply privately without public announcements.
- Message retrieval now verifies current Discord membership and channel permissions and stays within the request's channel and server, including for configured global administrators. Private threads require membership or thread-management permission; topic DMs recheck the recipient's current access.
- Server-removal cleanup across Vox's active databases, cached context, and voice transcripts, with offline reconciliation, persistent retry markers, and protection against delayed worker writes. Other servers, DMs, and global opt-outs are preserved; reminders now record their owning server.
- SQLCipher 4 storage for Vox's four databases, protected key-file loading, verified non-destructive migration, and encrypted deployment snapshots.
- Vox's public service privacy policy in `PRIVACY.md`, with operator contacts, data uses and providers, retention and deletion rights, opt-out status, and verified hosting controls.
- `/vox optout` opens a private, user-bound confirmation; confirming persists an account-wide opt-out, disables Vox features, removes active indexed messages and personal reminders/subscriptions, and excludes authored messages and voice from future processing and search. History and partial-edit safeguards prevent re-indexing withdrawn messages.
- Deployment review and shared-host migration plan for Jeeves, including SQLite preservation strategy for `discord-bots` (`deploy-plan.md`).
- Deployment assets for the shared host: `systemd` unit, production config template, runtime install helper, state staging helper, rsync excludes, and lean runtime requirements (`deploy/*`, `requirements.runtime.txt`).
- Docker build/run support.
- Safe URL fetch tooling for Vox with public-host validation, optional host blacklist controls, bounded JSON/text fetches, crawl4ai page extraction, and handle-based read/grep follow-ups.
- Event channels module with admin-managed subscriptions that poll ICS calendars and YouTube feeds and post new items into Discord channels.
- Indexed message search tool for querying the local Discord message index.
- SQLite-backed LLM throttling policy and request-event log with Discord role exemptions for keeping specific users in high reasoning mode.
- README docs for the LLM throttle schema, default seed values, and example `sqlite3` configuration commands.
- Expanded property-based test suite (Hypothesis) across Brave Search, rate limiting, typed_json coercion.
- QA runner `check.py` (with `check.sh` delegation) plus import-linter/coverage config updates.
- Local type stubs for emoji to improve type checking.
- Message reply references (reply-to ids) now persist in the background indexer.
- Commitment notes column plus a notes management tool for Vox reminders.
- Voice invite handler that joins voice channels and transcribes speakers via Whisper (separate module).
- Voice capture dependency for Discord voice receive plus PyNaCl runtime support.
- External API connectivity checker script (`servant/scripts/check_external_apis.py`) with IPv4/IPv6 probes via aiohttp.
### Removed
- Archived and removed the unused offline model-training and annotation applications, sample corpora, and their dependencies. Current Discord tools and scheduled features remain available; privacy cleanup still removes legacy conversation records.

### Changed
- Vox's production diagnostic journal and temporary exports now use bounded memory storage, with deployment templates preserving the journal namespace and isolated tmpfs mounts.
- Removed production plaintext databases, retired artifacts, and historical journal files after verifying all 93 files in a local recovery archive; retained server recovery archives and host swap are encrypted. Updated the public privacy policy to describe verified storage protections.
- Deployed Vox's privacy controls and SQLCipher databases on October 8, 2026, preserving the original databases and verified local rollback snapshots; privacy and deletion contact is now `wabbit@wabbit.one`. The service allows up to 256 MiB of locked memory for SQLCipher's protected allocations. The in-app privacy panel leads with moderator controls and reports the current channel setting.
- Vox requests no Presence access and makes Guild Members access opt-in; it skips roster polling without that intent and passive message backfill without Message Content access.
- Discord intent rejection now triggers one reconnect without privileged intents, keeping actual @mentions and DMs available while logging the limitations of bare v/vox triggers.
- Ordinary incoming message text is no longer written to application info logs.
- Added a Message Content review draft linked to Vox's public privacy policy; form answers lead with community benefits, moderator controls, and member privacy choices, with deployment and evidence notes kept distinct from proposed submission text.
- Runtime config loading now supports `JEEVES_CONFIG_PATH`, so deploys can point at `/etc/python-jeeves/private.yml` without relying on a working-directory config file.
- Topic subscriptions now use `fastembed` with the ONNX `sentence-transformers/all-MiniLM-L6-v2` model path, while preserving the legacy `all-MiniLM-L6-v2` config alias.
- Runtime dependency manifests now constrain `PyNaCl` to `<1.6` and pin the shared-host Discord voice stack in `requirements.runtime.txt`, matching what `discord.py[voice]` actually accepts for clean installs.
- Discord reply instructions now let Jeeves vary answer length based on the conversation's casualness and information needs.
- QA tooling now emits structured output, supports quiet/parallel runs, and improves coverage/error summaries.
- URL fetch now uses a dedicated browser-like user agent override instead of inheriting the global `web.user-agent`.
- JSON parsing and type checking tightened across Servant (import cycles now errors).
- Coverage fail-under is now 15 to align with current baseline results.
- Event-channel ICS initialization now publishes a bounded backfill window (last 7 days and next 7 days) instead of silent seeding.
- External API connectivity checker now probes Jupiter hosts and includes OPTIONS preflight checks.
- Interactive Vox replies now downgrade only `reasoning_effort` when the global LLM request throttle trips, while Discord administrators and configured exempt roles stay on high reasoning.
- New index DBs now seed the global LLM throttle row with a disabled but ready-to-enable default policy: more than 5 requests per hour downgraded to `low`, and untouched legacy seed rows migrate to that default.
- Discord message context now includes author permission metadata when known, so Jeeves can distinguish staff/admin users in conversation payloads.
- Voice transcriber now disconnects after 60 seconds with no non-bot members in the channel.
- Voice transcription defaults now chunk faster (shorter silence and max segment thresholds).
### Fixed
- Actual Discord mentions now invoke Vox by its account ID, independently of the account or personality name; v/vox aliases remain available across personalities, and failed mention lookups no longer prevent replies.
- Cross-user commitment lookups now default to the active request channel and reuse the current request guild context, so guild staff/owners can resolve and cancel another user's commitment without redundant channel metadata.
- Routine tasks now wait for Discord readiness, and dispatch helpers bind the active loop before startup work begins, avoiding first-boot reminder/event task failures during deployment.
- Background indexer now treats preserved-but-missing Discord channels as expected `not_found` cleanup instead of noisy error tracebacks while it disables stale rows from the carried-over SQLite state.
- Commitment check-ins now respect each commitment's `start_date`/`end_date` window instead of reminding future commitments early.
- Commitment DB access now serializes one-time schema init/backfills and waits on transient sqlite locks instead of rerunning write-backed init on every call.
- Commitment ownership overrides now recognize moderator/staff Discord permissions instead of requiring the `Administrator` bit.
- Background message payload indexing no longer wipes embeds/attachments/mentions on partial updates.
- Channel `extra_json` now keeps existing data and captures forum/voice/stage metadata.
- Message reply index migration no longer fails when upgrading older databases.
- Commitment creation now defaults `end_date` to 30 days after `start_date` when omitted.
- Voice transcriber sink now implements the required `wants_opus` hook and joins undeafened for audio capture.
- External API connectivity checker now reports request timeouts instead of crashing.

## [2026-01-14]
### Added
- CODI conversation disentanglement stack (Django app, API/client UI, tests, training scripts, datasets, docs). (6c688c2)
- Servant CODI integration: `servant/modules/codi_conversation_indexer.py` plus CODI tests in `tests/test_codi_conversation_indexer.py` and `tests/test_codi_disentangle.py`. (6c688c2)
- `typed_json` package for JSON coercion/type helpers, adopted across modules. (6c688c2)
- Full lint/test runner `check.sh` with mypy/pyright config files. (6c688c2)
- `servant/scripts/codi_run_channel.py` to run CODI indexing for a channel. (6c688c2)
### Changed
- Renamed entry point from `servant.py` to `servant_app.py`. (6c688c2)

## [2026-01-12]
### Added
- Background indexer module with sqlite schema and routine tasks to backfill/index Discord data. (519df57)
- Discord search tool for messages, members, and channels. (519df57)
- Brave Search API wrapper with rate limiting plus the `search_web` tool. (519df57)
- Topic subscriptions with sentence-transformers embeddings, sqlite storage, and cooldowns. (519df57)
- DB export tool that zips commitments/subscriptions tables and uploads to Discord. (519df57)

## [2026-01-11]
### Added
- YouTube transcript tool with URL/ID parsing and txt/srt/vtt/csv export. (cd559b5)
- `TypingIndicator` helper that adds a reaction and keeps Discord typing active while generating replies. (94b5333)
### Changed
- User messages are stored as JSON payloads (author, content, message_id, reply_to) and the system prompt now expects that format. (94b5333)
- Mention detection expanded to include single-letter name while avoiding URL false positives. (94b5333)
- Bot now replies when a user responds to one of its messages. (f88f280)
- Removed the `servant/modules/jove.py` personality module. (f88f280)
- `own_code_issues` pulls the GitHub token from `servant.defs`. (f88f280)

## [2026-01-30]
### Added
- Request-scoped context (`RequestContext`) with `ctx.with_request()` for tool permission checks. (unreleased)
- Permission helpers with global admin allowlist support for Discord tools. (unreleased)
- System-triggered Vox reply helper `ctx.request_vox_reply` for standard reply loop reuse. (unreleased)
### Changed
- Indexed message search and database export tools now require Discord administrator privileges. (unreleased)
- Commitment management now enforces ownership/admin rules for create/update/cancel/list. (unreleased)
- Renamed `ctx.secrets` to `ctx.config` for broader configuration storage. (unreleased)
- Commitment check-ins now use the standard Vox reply loop instead of direct channel sends. (unreleased)

## [2025-12-27]
### Added
- Repo introspection tools `own_code_ls`, `own_code_read`, and `own_code_grep` with root/path safeguards. (7cf8b39)
- GitHub issue creation tools for bug reports and feature requests with label handling. (7cf8b39)
- GitHub PR creation tool `own_code_submit_new_tool_pr` to add new tool modules under `servant/modules/`. (7cf8b39)
- Added `SECRET_GITHUB_TOKEN` to the secret registry. (7cf8b39)
### Changed
- System prompt now forbids @here/@everyone and points users to the GitHub repo for PRs; OpenAI calls request `reasoning_effort="high"`. (7cf8b39)
- `own_code_issues` now requires `servant.defs.SECRET_GITHUB_TOKEN` (removed fallback constant) and reads the token directly from ctx.config. (af21d8f, 9cce5ca)

## [2025-12-26]
### Added
- Commitment management module with sqlite storage plus a routine check-in task. (763255e)
- Routine task registration in module discovery. (763255e)
- YAML secrets template `.private.yml.tmpl`. (73e1f15)
- Cross-thread Discord send helper and a routine-task loop in `servant.py`. (73e1f15)
### Changed
- Secrets switched from `.private.clj` to `.private.yml` and renamed to hierarchical keys via `ALL_SECRETS`. (73e1f15)
- System prompt now includes channel name/id; tool execution wraps exceptions and returns success flags; model set to `gpt-5.2`. (763255e)
- Default personality renamed to Vox and message author info now includes mentions. (763255e)
- Commitment check-ins send messages via ctx.send_discord_message. (73e1f15)
### Fixed
- `get_current_datetime` now passes longitude/latitude in the correct order. (763255e)
### Removed
- Clojure config template and parser/exec modules (`.private.clj.tmpl`, `clj/*`). (73e1f15)

## [2025-06-11]
### Changed
- Reworked Jeeves into Jove with Python module-based tools under `servant/modules/`. (c866254)
### Added
- Tool modules for current_time, dalle, imgflip, reddit_jokes, switch_personality, weather, and wikipedia. (c866254)
- `servant/defs.py` module framework and supporting helpers. (c866254)
### Removed
- Legacy Jeeves Clojure/OpenAI scaffolding (`gpt.py`, `jeeves.clj`, old `servant/base` modules). (c866254)

## [2024-10-07]
### Added
- Initial Jeeves bot with Discord/OpenAI entry point `servant.py`. (2e8408a)
- Clojure-style S-expression parser/executor (`clj/*`) and base utilities (rate limiting, time parsing, weather). (2e8408a)
- Config template `.private.clj.tmpl` and license/docs scaffolding. (2e8408a)
