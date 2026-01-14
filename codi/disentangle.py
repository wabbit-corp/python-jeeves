from __future__ import annotations

import os
import sys
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from codi.api.model.disentanglement.feature import Feature
    from codi.api.model.disentanglement.model import Model as CodiModel


_MODEL_LOCK = threading.Lock()
_MODEL: CodiModel | None = None
_MODEL_DIR: Path | None = None
_CODI_IMPORT_ERROR: Exception | None = None


class NormalizedMessage(TypedDict):
    id: str
    authorId: str
    authorName: str
    content: str
    timestamp: str


class CommunityMessage(TypedDict):
    id: str
    authorId: str
    content: str
    timestamp: str


class CommunityMember(TypedDict):
    id: str
    name: str


class CommunityChannel(TypedDict):
    id: str
    path: str
    topics: list[object]
    messages: list[CommunityMessage]


class CommunityPayload(TypedDict):
    platform: str
    id: str
    name: str
    members: list[CommunityMember]
    channels: list[CommunityChannel]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _default_model_dir() -> Path:
    return _repo_root() / "codi" / "api" / "training" / "tmp" / "models"


def _ensure_codi_imported() -> None:
    global _CODI_IMPORT_ERROR
    if _CODI_IMPORT_ERROR is not None:
        raise RuntimeError(f"CODI import failed: {_CODI_IMPORT_ERROR}") from _CODI_IMPORT_ERROR

    if "codi.api.model.disentanglement.model" in sys.modules:
        return

    codi_pkg_root = _repo_root() / "codi"
    if not codi_pkg_root.exists():
        raise RuntimeError(f"CODI not found at {codi_pkg_root}")

    try:
        from importlib import import_module

        import_module("codi.api.model.disentanglement.model")
        import_module("codi.api.model.disentanglement.chat")
        import_module("codi.api.model.disentanglement.discourse")
        import_module("codi.api.model.disentanglement.content")
        import_module("codi.api.model.input.community")
    except Exception as exc:
        _CODI_IMPORT_ERROR = exc
        raise RuntimeError(f"CODI import failed: {exc}") from exc


def _get_model(model_dir: Path) -> CodiModel:
    global _MODEL, _MODEL_DIR
    with _MODEL_LOCK:
        if _MODEL is None or _MODEL_DIR != model_dir:
            os.environ["CODI_MODEL_DIR"] = str(model_dir)
            from importlib import import_module

            module = import_module("codi.api.model.disentanglement.model")
            model_cls = getattr(module, "Model", None)
            if model_cls is None or not isinstance(model_cls, type):
                raise RuntimeError("CODI Model class not found.")
            _MODEL = model_cls()
            _MODEL_DIR = model_dir
    assert _MODEL is not None
    return _MODEL


def _normalize_platform(value: object) -> str:
    if value is None:
        return "discord"
    platform = str(value).strip().lower()
    if platform not in {"discord", "slack"}:
        raise ValueError("platform must be 'discord' or 'slack'")
    return platform


def _resolve_features(names: Sequence[str] | None) -> list[type[Feature]]:
    from codi.api.model.disentanglement.chat import Chat
    from codi.api.model.disentanglement.content import Content
    from codi.api.model.disentanglement.discourse import Discourse

    if not names:
        names = ["chat", "discourse", "content"]
    normalized = [str(name).lower() for name in names]
    if "all" in normalized:
        normalized = ["chat", "discourse", "content"]

    features: list[type[Feature]] = []
    unknown = []

    for name in normalized:
        if name == "chat":
            features.extend(Chat.get_group_features())
        elif name == "discourse":
            features.extend(Discourse.get_group_features())
        elif name == "content":
            features.extend(Content.get_group_features())
        else:
            unknown.append(name)

    if unknown:
        raise ValueError(f"Unknown features: {', '.join(unknown)}")

    return features


def _first_present(message: dict[str, object], keys: Sequence[str]) -> object | None:
    for key in keys:
        if key in message and message[key] is not None:
            return message[key]
    return None


def _normalize_message(message: dict[str, object], index: int) -> NormalizedMessage:
    msg_id = _first_present(message, ["id", "message_id", "messageId"])
    if msg_id is None:
        msg_id = str(index)
    else:
        msg_id = str(msg_id)

    author_id = _first_present(message, ["author_id", "authorId", "author"])
    author_name = _first_present(message, ["author_name", "authorName", "author"])
    if isinstance(author_id, dict):
        author_obj = author_id
        author_id = _first_present(author_obj, ["id", "user_id", "userId", "author_id", "authorId"])
        author_name = _first_present(author_obj, ["name", "username", "display_name", "displayName"])

    if author_id is None:
        raise ValueError("message missing author_id")

    author_id = str(author_id)
    if author_name is None:
        author_name = author_id
    else:
        author_name = str(author_name)

    content = _first_present(message, ["content", "text", "body"])
    if content is None:
        raise ValueError("message missing content")
    content = str(content)

    timestamp = _first_present(message, ["timestamp", "created_at", "createdAt"])
    if timestamp is None:
        timestamp = str(index)
    elif isinstance(timestamp, (int, float)):
        timestamp = str(int(timestamp))
    else:
        timestamp = str(timestamp)

    return {
        "id": msg_id,
        "authorId": author_id,
        "authorName": author_name,
        "content": content,
        "timestamp": timestamp,
    }


def _build_community(
    messages: Sequence[dict[str, object]],
    platform: str,
    community_id: str,
    community_name: str,
    channel_id: str,
    channel_name: str,
) -> CommunityPayload:
    members: dict[str, CommunityMember] = {}
    normalized_messages: list[CommunityMessage] = []

    for idx, message in enumerate(messages):
        normalized = _normalize_message(message, idx)
        author_id = normalized["authorId"]
        author_name = normalized["authorName"]

        if author_id not in members:
            members[author_id] = {"id": author_id, "name": author_name}

        normalized_messages.append(
            {
                "id": normalized["id"],
                "authorId": author_id,
                "content": normalized["content"],
                "timestamp": normalized["timestamp"],
            }
        )

    return {
        "platform": platform,
        "id": community_id,
        "name": community_name,
        "members": list(members.values()),
        "channels": [
            {
                "id": channel_id,
                "path": channel_name,
                "topics": [],
                "messages": normalized_messages,
            }
        ],
    }
