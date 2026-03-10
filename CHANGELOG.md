# Changelog

All notable changes to the Servant project will be documented in this file.

The entries below are derived from the git history and grouped by release date.

## [Unreleased]
### Added
- CODI tooling and docs: auto-annotation, training export, and workflow notes (`conversations.md`).
- Active learning annotation CLI for message datasets with contextual display and JSONL annotations (`make_train_data.py`).
- Docker build/run support.
- Expanded property-based test suite (Hypothesis) across Brave Search, rate limiting, CODI parsing, statistics, and typed_json coercion.
- QA runner `check.py` (with `check.sh` delegation) plus import-linter/coverage config updates.
- Local type stubs for emoji/imblearn/sklearn to improve type checking.
- Discord status post for the annotation pipeline and dataset progress (`post.md`).
- Label examples from `labels.yml` now seed training with sample weights in `make_train_data.py`.
- Label cue overlap helper script (`label_cue_overlap.py`).
- Prefix rendering mode for per-author message chains in `make_train_data.py`, with render-spec tracking in embeddings and annotations.
- Optional colorized annotation output for contexts and suggestions in `make_train_data.py`.
- Random sampling and selection provenance fields for annotation batches in `make_train_data.py`.
- Annotation timestamps now include `annotated_at` in `make_train_data.py`.
- GPT-5.2 auto-annotation support with JSON schema output in `make_train_data.py`, showing auto labels before the prompt and accepting with `a`.
- Message reply references (reply-to ids) now persist in the background indexer.
- Video download/archive tooling: yt-dlp downloads, Discord attachment upload, and Google Drive OAuth/upload flow with per-user Drive storage in `VoxDownloads`.
### Changed
- QA tooling now emits structured output, supports quiet/parallel runs, and improves coverage/error summaries.
- JSON parsing and type checking tightened across CODI/Servant (import cycles now errors).
- Coverage fail-under is now 15 to align with current baseline results.
- make_train_data now reads labels from labels.yml, formats timestamps in America/New_York, and adds annotation autocomplete/suggestions.
- Word-salad weak labeling now uses a token Markov chain score with percentile thresholding to avoid over-labeling.
- Annotation batch selection now supports a round-robin strategy across labels (toggle via CLI).
- Weak label training now uses deterministic sample weights instead of randomized inclusion in `make_train_data.py`.
- Round-robin candidate selection now shortlists uncertain messages to avoid full sorts in `make_train_data.py`.
- Embedding cache keys now include render specs and rendered text hashes in `make_train_data.py`.
- Weak-label cue matching now normalizes punctuation for better recall in `make_train_data.py`.
- Annotation content hashes now follow the rendered text and mismatches are skipped by default in `make_train_data.py`.
- Round-robin selection now preserves deterministic ordering for stable seeded batches in `make_train_data.py`.
- Annotation batching now mixes in configurable random samples for evaluation-friendly labeling in `make_train_data.py`.
- Annotation sessions now retrain models every 10 new labels by default to refresh suggestions mid-batch.
- Annotation context now highlights weak label trigger spans in `make_train_data.py`.
- Startup timing logs now cover each initialization step before the first annotation in `make_train_data.py`.
- Weak label computation now skips entirely when `--weak-label-weight=0` to reduce startup time.
### Fixed
- Guarded unigram probability computation against empty or fully-trimmed vocabularies to avoid runtime crashes.
- Label cue matching now treats cues as regexes when needed and restores URL/link matchers in `make_train_data.py`.
- Fixed label input tokenization so label names are parsed as whole tokens.
- Excluded video file links from `~linkdrop` weak labeling in `make_train_data.py`.
- Label cue matching now requires explicit `re:` prefixes for regex patterns so literal punctuation cues match reliably.
- Timestamp formatting now supports ISO-8601 `created_at` values in `make_train_data.py`.
- Literal cue matching now avoids substring matches (e.g., `cat` in `concatenate`) in `make_train_data.py`.
- Prefix rendering now treats missing timestamps as boundaries in `make_train_data.py`.
- Model training now logs total duration in `make_train_data.py`.
- Auto-annotator schema now conforms to the Responses JSON schema subset in `make_train_data.py`.
- Background message payload indexing no longer wipes embeds/attachments/mentions on partial updates.
- Channel `extra_json` now keeps existing data and captures forum/voice/stage metadata.
- Message reply index migration no longer fails when upgrading older databases.

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

## [2025-12-27]
### Added
- Repo introspection tools `own_code_ls`, `own_code_read`, and `own_code_grep` with root/path safeguards. (7cf8b39)
- GitHub issue creation tools for bug reports and feature requests with label handling. (7cf8b39)
- GitHub PR creation tool `own_code_submit_new_tool_pr` to add new tool modules under `servant/modules/`. (7cf8b39)
- Added `SECRET_GITHUB_TOKEN` to the secret registry. (7cf8b39)
### Changed
- System prompt now forbids @here/@everyone and points users to the GitHub repo for PRs; OpenAI calls request `reasoning_effort="high"`. (7cf8b39)
- `own_code_issues` now requires `servant.secrets.SECRET_GITHUB_TOKEN` (removed fallback constant) and reads the token directly from ctx.secrets. (af21d8f, 9cce5ca)

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
