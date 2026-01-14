FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    VENV_PATH=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        g++ \
        gcc \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./

RUN python -m venv "$VENV_PATH" \
    && "$VENV_PATH/bin/pip" install --upgrade pip setuptools wheel

RUN python - <<'PY'
import tomllib
from pathlib import Path

data = tomllib.loads(Path("pyproject.toml").read_text())
deps = data.get("project", {}).get("dependencies", [])
Path("/tmp/requirements.txt").write_text("\n".join(deps) + "\n")
PY

RUN "$VENV_PATH/bin/pip" install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt \
    && "$VENV_PATH/bin/pip" check

FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    VENV_PATH=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

ARG APP_UID=10001
ARG APP_GID=10001

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r app --gid "$APP_GID" \
    && useradd -r -g app --uid "$APP_UID" --create-home --home-dir /home/app --shell /usr/sbin/nologin app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app . ./

RUN mkdir -p /app/codi/api/training/tmp/models \
    && chown -R app:app /app

USER app

CMD ["python", "servant_app.py"]
