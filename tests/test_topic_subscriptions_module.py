from __future__ import annotations

import asyncio
import math
import sys
import types
from collections.abc import Generator
from pathlib import Path

import pytest

from servant.defs import GlobalContext
from servant.modules import topic_subscriptions
from typed_json import JSONDict


def _ctx_with_db(tmp_path: Path) -> GlobalContext:
    config: JSONDict = {
        "topic_subscriptions_db_path": str(tmp_path / "topic_subscriptions.sqlite3"),
    }
    return GlobalContext(config=config)


@pytest.fixture(autouse=True)
def _reset_model_cache() -> Generator[None, None, None]:
    topic_subscriptions._MODEL = None
    topic_subscriptions._MODEL_NAME = None
    yield
    topic_subscriptions._MODEL = None
    topic_subscriptions._MODEL_NAME = None


class _FakeEmbeddingVector:
    def __init__(self, values: list[float]) -> None:
        self._values = values

    def tolist(self) -> list[float]:
        return list(self._values)


class _FakeTextEmbedding:
    init_model_names: list[str] = []
    embedded_documents: list[list[str]] = []

    def __init__(self, *, model_name: str) -> None:
        self.model_name = model_name
        self.init_model_names.append(model_name)

    def embed(
        self,
        documents: list[str],
        *,
        batch_size: int = 256,
        parallel: int | None = None,
    ) -> list[_FakeEmbeddingVector]:
        del batch_size, parallel
        self.embedded_documents.append(list(documents))
        return [_FakeEmbeddingVector([3.0, 4.0]) for _ in documents]


def _install_fake_fastembed(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_module = types.ModuleType("fastembed")
    setattr(fake_module, "TextEmbedding", _FakeTextEmbedding)
    monkeypatch.setitem(sys.modules, "fastembed", fake_module)


def test_get_model_uses_fastembed_and_legacy_model_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeTextEmbedding.init_model_names.clear()
    _install_fake_fastembed(monkeypatch)
    ctx = GlobalContext()

    model = topic_subscriptions._get_model(ctx)
    same_model = topic_subscriptions._get_model(ctx)

    assert model is same_model
    assert _FakeTextEmbedding.init_model_names == ["sentence-transformers/all-MiniLM-L6-v2"]


def test_embed_text_normalizes_fastembed_vectors(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeTextEmbedding.init_model_names.clear()
    _FakeTextEmbedding.embedded_documents.clear()
    _install_fake_fastembed(monkeypatch)
    ctx = GlobalContext()

    vec = topic_subscriptions._embed_text(ctx, "topic text")

    assert vec == pytest.approx([0.6, 0.8])
    assert math.isclose(sum(value * value for value in vec), 1.0)
    assert _FakeTextEmbedding.embedded_documents == [["topic text"]]


def test_topic_subscription_manage_create_and_match(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ctx = _ctx_with_db(tmp_path)

    embeddings = {
        "cats": [1.0, 0.0],
        "cats are neat": [1.0, 0.0],
        "dogs are neat": [0.0, 1.0],
    }

    def _fake_embed_text(_ctx: GlobalContext, text: str) -> list[float]:
        try:
            return embeddings[text]
        except KeyError as exc:  # pragma: no cover - defensive in test helper
            raise AssertionError(f"Unexpected embedding request: {text}") from exc

    monkeypatch.setattr(topic_subscriptions, "_embed_text", _fake_embed_text)

    create_result = asyncio.run(
        topic_subscriptions.topic_subscription_manage(
            ctx,
            {
                "operation": "create",
                "topic": "cats",
                "user_id": "2002",
                "guild_id": "3003",
                "channel_id": "4004",
                "similarity_threshold": 0.75,
            },
        )
    )

    assert create_result["ok"] is True

    notifications = topic_subscriptions._match_subscriptions(
        ctx,
        guild_id="3003",
        channel_id="4004",
        author_id="1001",
        content="cats are neat",
        now_ms=123,
    )
    assert notifications == [
        {
            "user_id": "2002",
            "topics": [{"topic": "cats", "similarity": 1.0}],
        }
    ]

    no_notifications = topic_subscriptions._match_subscriptions(
        ctx,
        guild_id="3003",
        channel_id="4004",
        author_id="1001",
        content="dogs are neat",
        now_ms=456,
    )
    assert no_notifications == []
