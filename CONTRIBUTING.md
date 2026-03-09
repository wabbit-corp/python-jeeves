# Contributing

## Required checks

Before opening a PR, run:

```bash
python -m pytest -q
python -m ruff check .
python -m black --check .
python -m mypy .
mkdocs build --strict
```

Documentation updates are required for user-visible behavior changes.
