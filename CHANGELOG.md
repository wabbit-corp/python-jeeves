# Changelog

All notable changes to the Servant project will be documented in this file.

The entries below are derived from the git history and grouped by release date.

## [Unreleased]
### Added
- Property-based tests for commitment and Brave Search helper logic.
- Property-based tests for Brave Search API response stripping and search parameters.
- Property-based tests for rate limiting, CODI message/annot parsing, and statistics utilities.
- Strict typed_json coercion error-path tests.
- CODI conversation and training export workflow documentation (`conversations.md`).
- Hypothesis test dependency for coverage-focused testing (deptry ignore for test-only use).
- Docker build/run support with `Dockerfile`, `docker-compose.yml`, and `.dockerignore`.
- CODI auto-annotation tooling (`servant/modules/codi_auto_annotator.py`, `servant/scripts/codi_auto_annotate.py`).
- CODI training export script from the indexer database (`servant/scripts/export_codi_training.py`).
- Local type stubs for emoji/imblearn/sklearn to improve type checking.
- Python QA runner `check.py` mirroring `check.sh` output and behavior.
- Parser self-tests in `check.py` using captured tool output fixtures.
- Import-linter contracts and coverage configuration in `pyproject.toml`.
- Property-based tests for typed_json coercion and CODI mention/content parsing branches.
### Changed
- QA runner entrypoint is now `check.py`; `check.sh` delegates to it.
- QA runner `check.sh` now strips OSC/CSI sequences in logs, runs import-linter when available, and improves diff-cover/coverage summaries.
- `check.py` now models tool output with per-tool issue dataclasses and parses JSON output when available.
- `check.py` now requests mypy JSON output, parses diff-cover JSON reports, and consumes pytest JUnit XML for structured results.
- `check.py` now supports quiet mode (SHOW_OUTPUT) and parallel execution (QA_JOBS) with a unified error table.
- `check.py` now writes coverage XML into the log directory, parses branch coverage from Cobertura output, and prints a branch-first summary.
- `check.py` unified errors now render source snippets with caret ranges and optional ANSI highlighting.
- `check.py` bandit parsing now converts column offsets to 1-based spans for accurate caret ranges.
- CODI/Servant JSON parsing tightened with `typed_json` coercion across models and views.
- Pyright now reports import cycles as errors.
- `check.py` now parses tool output into structured issues and prints normalized findings.
- Coverage fail-under is now 15 to align with current baseline results.
- `check.py` color output no longer depends on termcolor.
### Fixed
- Broke CODI import cycles by moving community serialization into the model layer and using lightweight protocol types.
- Centralized disentanglement feature registration/extraction to avoid feature module circular imports.
- Tightened Brave Search tests to satisfy type checking and import ordering.
- Updated test typing annotations and emoji stubs to satisfy mypy/pyright, and pointed pyright at the local `.venv`.
- `check.py` now fails the suite when parsed error issues exist even if tools exit 0, and JSON payload extraction prefers the last payload (including JSON Lines).
- `check.py` mypy parsing avoids name redefinition warnings and diff-cover uses the non-deprecated JSON format flag.
- `check.py` unified errors no longer drop warnings on failed tools and now formats diff-cover/coverage/vulture issues cleanly.
- `check.py` diff-cover file issues now omit non-violating files and show file names in unified error spans.
- `check.py` summary states now pad before coloring for stable table alignment.
- `check.py` diff-cover JSON parsing reports threshold failures; unified errors now sort issues and underline multi-line spans to end-of-line.
- Guarded unigram probability computation against empty or fully-trimmed vocabularies to avoid runtime crashes.

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
