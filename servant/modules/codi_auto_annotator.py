from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from openai import AsyncOpenAI

from typed_json import JSON, JSONDict, coerce_str, obj_to_json, require_obj

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class AnnotatorConfig:
    chunk_size: int
    overlap: int
    min_overlap_ratio: float
    min_overlap_messages: int
    max_message_chars: int
    model: str = "gpt-5.2"
    max_completion_tokens: int = 6000
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh"] = "low"
    retries: int = 1


@dataclass(frozen=True)
class PromptMessage:
    message_id: str
    author: str
    timestamp: str
    content: str


def _first_present(message: JSONDict, keys: Sequence[str]) -> object | None:
    for key in keys:
        if key in message and message[key] is not None:
            return message[key]
    return None


def _coerce_int(value: object | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
            return int(text)
    return None


def _normalize_timestamp(message: JSONDict, fallback: int) -> int:
    raw_ts = _first_present(message, ["timestamp_ms", "timestamp", "created_at"])
    ts = _coerce_int(raw_ts)
    if ts is None:
        return fallback
    if ts < 10_000_000_000:
        return ts * 1000
    return ts


def _collapse_whitespace(text: str) -> str:
    return " ".join(text.split())


def _message_sort_key(item: tuple[int, JSONDict]) -> tuple[int, int]:
    index, message = item
    return (_normalize_timestamp(message, index), index)


def _chunk_indices(total: int, chunk_size: int, overlap: int) -> list[tuple[int, int]]:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive.")
    if overlap <= 0:
        raise ValueError("overlap must be positive.")
    if overlap >= chunk_size:
        raise ValueError("overlap must be smaller than chunk_size.")

    step = chunk_size - overlap
    indices: list[tuple[int, int]] = []
    start = 0
    while start < total:
        end = min(start + chunk_size, total)
        indices.append((start, end))
        if end == total:
            break
        start += step
    return indices


def _to_prompt_message(message: JSONDict) -> PromptMessage:
    message_id = coerce_str(message.get("id"), field="id")
    author_value = _first_present(message, ["authorName", "author_name", "authorId", "author_id"])
    author = str(author_value) if author_value is not None else "unknown"
    content_value = message.get("content")
    content = str(content_value) if content_value is not None else ""
    timestamp_value = _first_present(message, ["timestamp_ms", "timestamp", "created_at"])
    timestamp = str(timestamp_value) if timestamp_value is not None else ""
    return PromptMessage(
        message_id=message_id,
        author=author,
        timestamp=timestamp,
        content=content,
    )


def _format_prompt(messages: Sequence[PromptMessage], max_chars: int) -> str:
    lines: list[str] = []
    for msg in messages:
        content = _collapse_whitespace(msg.content)
        if max_chars > 0 and len(content) > max_chars:
            content = content[: max_chars - 3] + "..."
        lines.append(f"{msg.message_id} | {msg.author} | {msg.timestamp} | {content}")
    return "\n".join(lines)


def _parse_assignments(
    text: str,
    message_ids: set[str],
) -> dict[str, str]:
    payload: JSON
    try:
        payload = obj_to_json(json.loads(text))
    except json.JSONDecodeError as exc:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError("Model response was not valid JSON.") from exc
        try:
            payload = obj_to_json(json.loads(text[start : end + 1]))
        except json.JSONDecodeError as exc2:
            raise ValueError("Model response was not valid JSON.") from exc2

    assignments_raw: list[JSON] | None = None
    if isinstance(payload, list):
        assignments_raw = payload
    elif isinstance(payload, dict):
        value = payload.get("assignments")
        if isinstance(value, list):
            assignments_raw = value

    if assignments_raw is None:
        raise ValueError("Model response missing assignments.")

    assignments: dict[str, str] = {}
    for item in assignments_raw:
        if not isinstance(item, dict):
            continue
        entry = require_obj(item)
        msg_id = coerce_str(entry.get("id"), field="id")
        conv_value = _first_present(entry, ["conversation_id", "conversation"])
        conv_id = coerce_str(conv_value, field="conversation_id")
        assignments[msg_id] = conv_id

    missing = message_ids.difference(assignments.keys())
    if missing:
        raise ValueError(f"Model response missing {len(missing)} message assignments.")

    return assignments


async def _annotate_chunk(
    client: AsyncOpenAI,
    messages: Sequence[JSONDict],
    config: AnnotatorConfig,
) -> dict[str, str]:
    prompt_messages = [_to_prompt_message(message) for message in messages]
    message_ids = {msg.message_id for msg in prompt_messages}
    prompt = _format_prompt(prompt_messages, config.max_message_chars)

    system_prompt = (
        "You are labeling consecutive chat messages into conversation threads. "
        "Return JSON only. "
        'Output format: {"assignments": [{"id": "<message_id>", "conversation_id": "C1"}, ...]}. '
        "Use C1, C2, ... to denote distinct conversations within this chunk."
    )
    user_prompt = f"Messages:\n{prompt}\nReturn JSON with assignments for every message id."

    last_error: Exception | None = None
    for attempt in range(config.retries + 1):
        try:
            response = await client.chat.completions.create(
                model=config.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                reasoning_effort=config.reasoning_effort,
                max_completion_tokens=config.max_completion_tokens,
            )
            content = response.choices[0].message.content if response.choices else None
            if content is None:
                raise ValueError("Model response missing content.")
            return _parse_assignments(content, message_ids)
        except Exception as exc:  # noqa: BLE001 - surfaced as a failure after retries
            last_error = exc
            _LOGGER.warning("Annotation attempt %s failed: %s", attempt + 1, exc)

    if last_error is None:
        raise ValueError("Annotation failed without error.")
    raise last_error


def _map_local_to_global(
    assignments: dict[str, str],
    message_ids: Sequence[str],
    global_labels: dict[str, str],
    *,
    min_overlap_ratio: float,
    min_overlap_messages: int,
    next_label: int,
) -> tuple[dict[str, str], int]:
    local_labels = sorted(set(assignments.values()))
    local_to_global: dict[str, str] = {}

    for local_label in local_labels:
        overlap_ids = [
            msg_id for msg_id in message_ids if msg_id in global_labels and assignments.get(msg_id) == local_label
        ]
        if overlap_ids:
            counts = Counter(global_labels[msg_id] for msg_id in overlap_ids)
            best_label, best_count = counts.most_common(1)[0]
            ratio = best_count / len(overlap_ids)
            if best_count >= min_overlap_messages and ratio >= min_overlap_ratio:
                local_to_global[local_label] = best_label
                continue

        local_to_global[local_label] = f"T{next_label}"
        next_label += 1

    return local_to_global, next_label


async def annotate_channel_messages(
    client: AsyncOpenAI,
    channel: JSONDict,
    config: AnnotatorConfig,
) -> JSONDict:
    messages_value = channel.get("messages")
    if not isinstance(messages_value, list):
        return channel

    messages: list[JSONDict] = []
    for item in messages_value:
        if isinstance(item, dict):
            messages.append(require_obj(item))

    if not messages:
        return channel

    ordered = sorted(list(enumerate(messages)), key=_message_sort_key)
    ordered_messages = [message for _, message in ordered]
    ordered_ids = [coerce_str(message.get("id"), field="id") for message in ordered_messages]

    chunk_indices = _chunk_indices(len(ordered_messages), config.chunk_size, config.overlap)

    global_labels: dict[str, str] = {}
    conflicts: list[JSON] = []
    next_label = 1
    for start, end in chunk_indices:
        chunk_messages = ordered_messages[start:end]
        chunk_ids = ordered_ids[start:end]
        assignments = await _annotate_chunk(client, chunk_messages, config)
        local_to_global, next_label = _map_local_to_global(
            assignments,
            chunk_ids,
            global_labels,
            min_overlap_ratio=config.min_overlap_ratio,
            min_overlap_messages=config.min_overlap_messages,
            next_label=next_label,
        )

        for msg_id in chunk_ids:
            local_label = assignments[msg_id]
            global_label = local_to_global[local_label]
            existing = global_labels.get(msg_id)
            if existing is not None and existing != global_label:
                conflicts.append(
                    {
                        "message_id": msg_id,
                        "existing": existing,
                        "new": global_label,
                    }
                )
                continue
            global_labels[msg_id] = global_label

    for message in messages:
        msg_id = coerce_str(message.get("id"), field="id")
        label = global_labels.get(msg_id)
        if label is None:
            continue
        message["conversation"] = label
        message["conversation_id"] = label

    metadata_value = channel.get("metadata")
    metadata: JSONDict
    if isinstance(metadata_value, dict):
        metadata = require_obj(metadata_value)
    else:
        metadata = {}

    metadata["auto_annotator"] = {
        "chunks": len(chunk_indices),
        "chunk_size": config.chunk_size,
        "overlap": config.overlap,
        "min_overlap_ratio": config.min_overlap_ratio,
        "min_overlap_messages": config.min_overlap_messages,
        "model": config.model,
        "conflicts": conflicts,
    }
    channel["metadata"] = metadata
    return channel


async def annotate_community(
    client: AsyncOpenAI,
    community: JSONDict,
    config: AnnotatorConfig,
    *,
    channel_ids: Sequence[str] | None = None,
) -> JSONDict:
    channels_value = community.get("channels")
    if not isinstance(channels_value, list):
        raise ValueError("community channels must be a list.")

    wanted = {str(item) for item in channel_ids} if channel_ids else None
    channels: list[JSON] = []
    for item in channels_value:
        if not isinstance(item, dict):
            channels.append(item)
            continue
        channel = require_obj(item)
        channel_id = channel.get("id")
        if wanted is not None and channel_id is not None and str(channel_id) not in wanted:
            channels.append(channel)
            continue
        updated = await annotate_channel_messages(client, channel, config)
        channels.append(updated)

    community["channels"] = channels
    community_metadata_value = community.get("metadata")
    community_metadata: JSONDict
    if isinstance(community_metadata_value, dict):
        community_metadata = require_obj(community_metadata_value)
    else:
        community_metadata = {}

    community_metadata["auto_annotator"] = {
        "chunk_size": config.chunk_size,
        "overlap": config.overlap,
        "min_overlap_ratio": config.min_overlap_ratio,
        "min_overlap_messages": config.min_overlap_messages,
        "model": config.model,
    }
    community["metadata"] = community_metadata
    return community
