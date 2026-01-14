Use .venv python venv.
Use `./check.sh [path]` for the full lint/type/test suite (ruff, black --check, mypy, pyright/basedpyright, pytest/unittest, deptry, vulture, semgrep, bandit, pip-audit).
Run tests (especially relevant tests) regularly while editing.
Always keep CHANGELOG.md up to date with meaningful entries.

Python coding rules:
1. do not use casts unless absolutely necessary
2. do not use type: ignore comments unless absolutely necessary
3. prefer explicit imports over wildcard imports
4. you may user typed_json.JSON (see `typed_json/__init__.py`) for json
5. prefer hypotheses / property-based tests where applicable
6. make your code as acyclic as possible (avoid circular imports)
7. practice defensive programming (validate inputs using assertions or explicit checks, validate state)
8. use logging with appropriate log levels instead of print statements
9. use explicit dependency injection (pass dependencies as parameters) where applicable
