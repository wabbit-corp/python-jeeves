from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

_MODEL_LOCK = threading.Lock()
_MODEL = None
_MODEL_DIR: Optional[Path] = None
_CODI_IMPORT_ERROR: Optional[Exception] = None


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _default_model_dir() -> Path:
    return _repo_root() / "codi" / "api" / "training" / "tmp" / "models"


def _ensure_codi_imported() -> None:
    global _CODI_IMPORT_ERROR
    if _CODI_IMPORT_ERROR is not None:
        raise RuntimeError(f"CODI import failed: {_CODI_IMPORT_ERROR}") from _CODI_IMPORT_ERROR

    if "api.model.disentanglement.model" in sys.modules:
        return

    codi_pkg_root = _repo_root() / "codi"
    if not codi_pkg_root.exists():
        raise RuntimeError(f"CODI not found at {codi_pkg_root}")

    if str(codi_pkg_root) not in sys.path:
        sys.path.insert(0, str(codi_pkg_root))

    try:
        from api.model.disentanglement.model import Model  # type: ignore[import-not-found]  # noqa: F401
        from api.model.disentanglement.chat import Chat  # type: ignore[import-not-found]  # noqa: F401
        from api.model.disentanglement.discourse import Discourse  # type: ignore[import-not-found]  # noqa: F401
        from api.model.disentanglement.content import Content  # type: ignore[import-not-found]  # noqa: F401
        from api.model.input.community import Community  # type: ignore[import-not-found]  # noqa: F401
    except Exception as exc:
        _CODI_IMPORT_ERROR = exc
        raise RuntimeError(f"CODI import failed: {exc}") from exc


def _get_model(model_dir: Path):
    global _MODEL, _MODEL_DIR
    with _MODEL_LOCK:
        if _MODEL is None or _MODEL_DIR != model_dir:
            os.environ["CODI_MODEL_DIR"] = str(model_dir)
            from api.model.disentanglement.model import Model

            _MODEL = Model()
            _MODEL_DIR = model_dir
    return _MODEL


def _normalize_platform(value: Any) -> str:
    if value is None:
        return "discord"
    platform = str(value).strip().lower()
    if platform not in {"discord", "slack"}:
        raise ValueError("platform must be 'discord' or 'slack'")
    return platform


def _resolve_features(names: Optional[List[str]]):
    from api.model.disentanglement.chat import Chat
    from api.model.disentanglement.discourse import Discourse
    from api.model.disentanglement.content import Content

    if not names:
        names = ["chat", "discourse", "content"]
    normalized = [str(name).lower() for name in names]
    if "all" in normalized:
        normalized = ["chat", "discourse", "content"]

    features = []
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


def _first_present(message: Dict[str, Any], keys: List[str]) -> Any:
    for key in keys:
        if key in message and message[key] is not None:
            return message[key]
    return None


def _normalize_message(message: Dict[str, Any], index: int) -> Dict[str, Any]:
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
    messages: List[Dict[str, Any]],
    platform: str,
    community_id: str,
    community_name: str,
    channel_id: str,
    channel_name: str,
) -> Dict[str, Any]:
    members: Dict[str, Dict[str, str]] = {}
    normalized_messages: List[Dict[str, Any]] = []

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
