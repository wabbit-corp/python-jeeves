# Development

Run these checks before opening a PR:

```bash
python -m pytest -q
python -m ruff check .
python -m black --check .
python -m mypy .
mkdocs build --strict
```
