#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib
import json
import logging
import math
import os
import random
import re
import sys
import time
from collections import Counter, deque
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

import numpy as np
import yaml
from numpy.typing import NDArray
from sentence_transformers import SentenceTransformer
from sklearn.linear_model import LogisticRegression

from typed_json import coerce_bool, coerce_int, coerce_optional_str_list, coerce_str

LOGGER = logging.getLogger(__name__)

FloatArray = NDArray[np.float32]
IntArray = NDArray[np.int64]
Matcher = Callable[[str], bool]
_AUTOCOMPLETE_WARNED = False
_SUGGESTION_WARNED = False

if TYPE_CHECKING:
    from openai import OpenAI
    from openai.types.responses.response import Response

try:
    NY_TZ: dt.tzinfo = ZoneInfo("America/New_York")
except Exception as exc:  # pragma: no cover - fallback for missing tzdata
    LOGGER.warning("Falling back to UTC: %s", exc)
    NY_TZ = dt.timezone.utc

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
DISCORD_INVITE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:discord\.gg|discord(?:app)?\.com/invite)/[\w-]+",
    re.IGNORECASE,
)
ZIP_FILE_RE = re.compile(r"https?://\S+\.zip(?:\b|\?|#)", re.IGNORECASE)
EXE_FILE_RE = re.compile(r"https?://\S+\.exe(?:\b|\?|#)", re.IGNORECASE)
APK_FILE_RE = re.compile(r"https?://\S+\.apk(?:\b|\?|#)", re.IGNORECASE)
VIDEO_FILE_RE = re.compile(
    r"\.(mp4|mov|webm|mkv|avi|m4v|mpg|mpeg|ogv|3gp|3g2)(?:\b|\?|#)",
    re.IGNORECASE,
)
TOKEN_RE = re.compile(r"[a-z0-9']+")

HIGH_ENTROPY_MIN_TOKENS = 12
HIGH_ENTROPY_PERCENTILE = 0.99
ROUND_ROBIN_SHORTLIST_MULTIPLIER = 5
LABEL_EXAMPLE_WEIGHT = 0.2
PREFIX_SEPARATOR = "\n"
AUTO_ANNOTATOR_DEFAULT_MAX_OUTPUT_TOKENS = 400

TermColorFn = Callable[[str, str | None, str | None, Sequence[str] | None], str]
_term_colored: TermColorFn | None = None
_COLOR_ENABLED = False

INLINE_CODE_RE = re.compile(r"`[^`]+`")
DISCORD_MENTION_RE = re.compile(r"<@!?\d+>|<@&\d+>|(?:(?<=\s)|^)@[A-Za-z0-9_.-]{2,}")
WEAK_HIGHLIGHT_PRIORITY = -1
WEAK_HIGHLIGHT_COLOR = "white"
WEAK_HIGHLIGHT_ON_COLOR = "on_blue"
WEAK_HIGHLIGHT_ATTRS = ("bold",)
WEAK_CUE_HIGHLIGHT_PATTERNS = {
    "~discord_invite": DISCORD_INVITE_RE,
    "~linkdrop": URL_RE,
    "~zip_file_link": ZIP_FILE_RE,
    "~exe_file_link": EXE_FILE_RE,
    "~apk_file_link": APK_FILE_RE,
}


@dataclass(frozen=True)
class _HighlightSpan:
    start: int
    end: int
    color: str | None
    on_color: str | None
    attrs: tuple[str, ...]
    priority: int


def configure_color_output(*, force: bool | None = None) -> None:
    global _COLOR_ENABLED
    _load_termcolor()
    if _term_colored is None:
        _COLOR_ENABLED = False
        return
    if force is not None:
        _COLOR_ENABLED = force
        return
    if os.environ.get("NO_COLOR") is not None:
        _COLOR_ENABLED = False
        return
    _COLOR_ENABLED = sys.stderr.isatty()


def _load_termcolor() -> None:
    global _term_colored
    if _term_colored is not None:
        return
    try:
        module = importlib.import_module("termcolor")
    except ImportError:  # pragma: no cover - optional dependency
        return
    colored = getattr(module, "colored", None)
    if callable(colored):
        colored_fn = cast(TermColorFn, colored)

        def _wrapped(
            text: str,
            color: str | None = None,
            on_color: str | None = None,
            attrs: Sequence[str] | None = None,
        ) -> str:
            result = colored_fn(text, color, on_color, attrs)
            return result if isinstance(result, str) else text

        _term_colored = _wrapped


def _c(
    text: str,
    *,
    color: str | None = None,
    on_color: str | None = None,
    attrs: Sequence[str] = (),
) -> str:
    if not _COLOR_ENABLED or _term_colored is None:
        return text
    attrs_list = list(attrs) if attrs else None
    return _term_colored(text, color, on_color, attrs_list)


def _collect_spans(text: str) -> list[_HighlightSpan]:
    spans: list[_HighlightSpan] = []

    def add(
        pattern: re.Pattern[str],
        *,
        color: str | None,
        on_color: str | None,
        attrs: tuple[str, ...],
        priority: int,
    ) -> None:
        for match in pattern.finditer(text):
            if match.end() <= match.start():
                continue
            spans.append(_HighlightSpan(match.start(), match.end(), color, on_color, attrs, priority))

    add(INLINE_CODE_RE, color="green", on_color=None, attrs=("bold",), priority=0)
    add(DISCORD_INVITE_RE, color="magenta", on_color=None, attrs=("underline", "bold"), priority=1)
    add(EXE_FILE_RE, color="red", on_color=None, attrs=("underline", "bold"), priority=2)
    add(APK_FILE_RE, color="red", on_color=None, attrs=("underline", "bold"), priority=2)
    add(ZIP_FILE_RE, color="yellow", on_color=None, attrs=("underline", "bold"), priority=2)
    add(URL_RE, color="blue", on_color=None, attrs=("underline",), priority=3)
    add(DISCORD_MENTION_RE, color="cyan", on_color=None, attrs=("bold",), priority=4)

    spans.sort(key=lambda span: (span.start, span.priority, -(span.end - span.start)))
    accepted: list[_HighlightSpan] = []
    last_end = 0
    for span in spans:
        if span.start < last_end:
            continue
        accepted.append(span)
        last_end = span.end
    return accepted


def highlight_text(text: str) -> str:
    if not _COLOR_ENABLED or not text:
        return text
    spans = _collect_spans(text)
    return _apply_highlight_spans(text, spans)


def _clamp_spans(spans: Sequence[_HighlightSpan], *, max_len: int) -> list[_HighlightSpan]:
    clamped: list[_HighlightSpan] = []
    for span in spans:
        start = max(0, min(span.start, max_len))
        end = max(start, min(span.end, max_len))
        if start >= end:
            continue
        clamped.append(
            _HighlightSpan(
                start=start,
                end=end,
                color=span.color,
                on_color=span.on_color,
                attrs=span.attrs,
                priority=span.priority,
            )
        )
    return clamped


def _apply_highlight_spans(text: str, spans: Sequence[_HighlightSpan]) -> str:
    if not spans:
        return text
    spans_sorted = sorted(spans, key=lambda span: (span.start, span.priority, -(span.end - span.start)))
    accepted: list[_HighlightSpan] = []
    last_end = 0
    for span in spans_sorted:
        if span.start < last_end:
            continue
        accepted.append(span)
        last_end = span.end
    out: list[str] = []
    last = 0
    for span in accepted:
        if span.start > last:
            out.append(text[last : span.start])
        out.append(
            _c(
                text[span.start : span.end],
                color=span.color,
                on_color=span.on_color,
                attrs=span.attrs,
            )
        )
        last = span.end
    if last < len(text):
        out.append(text[last:])
    return "".join(out)


def highlight_text_with_spans(text: str, extra_spans: Sequence[_HighlightSpan] | None) -> str:
    if not _COLOR_ENABLED or not text:
        return text
    spans = _collect_spans(text)
    if extra_spans:
        spans.extend(_clamp_spans(extra_spans, max_len=len(text)))
    return _apply_highlight_spans(text, spans)


def _prob_bar(p: float, *, width: int = 12) -> str:
    p = 0.0 if math.isnan(p) else max(0.0, min(1.0, p))
    filled = int(round(p * width))
    return "[" + ("=" * filled) + (" " * (width - filled)) + "]"


def _prob_color(p: float) -> str:
    if p >= 0.85:
        return "green"
    if p >= 0.65:
        return "yellow"
    if p >= 0.50:
        return "cyan"
    if p >= 0.30:
        return "magenta"
    return "red"


@dataclass(frozen=True)
class LabelDefinition:
    name: str
    description: str
    patterns: tuple[re.Pattern[str], ...]
    matchers: tuple[Matcher, ...]
    special_cues: tuple[str, ...]
    positive_examples: tuple[str, ...] = ()
    negative_examples: tuple[str, ...] = ()


@dataclass(frozen=True)
class RenderSpec:
    mode: str
    prefix_max_messages: int | None
    prefix_max_gap_seconds: int | None
    prefix_max_chars: int | None
    boundary_regex: str | None


@dataclass(frozen=True)
class Message:
    index: int
    message_id: str
    channel_id: str
    author_id: str
    content: str
    created_at: str | None
    deleted_at: str | None
    mention_ids: list[str]
    attachment_count: int


@dataclass(frozen=True)
class AnnotationRecord:
    message_id: str
    channel_id: str
    labels: list[str]
    label_names: list[str]
    created_at: str
    annotated_at: str | None
    content_hash: str
    annotator: str | None
    render_spec: dict[str, object] | None
    selection_strategy: str | None
    selection_reason: str | None
    randomly_chosen: bool | None


@dataclass(frozen=True)
class SelectedCandidate:
    index: int
    selection_strategy: str
    selection_reason: str
    randomly_chosen: bool


@dataclass(frozen=True)
class AutoAnnotator:
    client: OpenAI
    model: str
    max_output_tokens: int
    schema: dict[str, object]
    label_names: tuple[str, ...]


@dataclass(frozen=True)
class EvalEntry:
    index: int
    labels: frozenset[str]
    label_names: frozenset[str]


@dataclass(frozen=True)
class EvalMetrics:
    label: str
    support: int
    positives: int
    tp: int
    fp: int
    fn: int
    tn: int
    precision: float | None
    recall: float | None
    f1: float | None
    accuracy: float | None
    status: str


class QuitAnnotation(Exception):
    pass


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Active learning annotation pipeline for message datasets.",
    )
    parser.add_argument("--input", type=Path, default=Path("message_dump.jsonl"))
    parser.add_argument(
        "--annotations",
        type=Path,
        default=Path("data/message_annotations.jsonl"),
    )
    parser.add_argument(
        "--label-config",
        type=Path,
        default=Path("labels.yml"),
        help="Path to YAML label definitions.",
    )
    parser.add_argument("--embedding-model", default="all-MiniLM-L6-v2")
    parser.add_argument(
        "--embedding-cache",
        type=Path,
        default=Path("data/message_embeddings.npy"),
    )
    parser.add_argument("--no-embedding-cache", action="store_true")
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--weak-label-weight", type=float, default=0.2)
    parser.add_argument(
        "--retrain-interval",
        type=int,
        default=10,
        help="Retrain models after this many new annotations (0 to disable).",
    )
    parser.add_argument(
        "--random-sample-fraction",
        type=float,
        default=0.2,
        help="Fraction of each batch to sample uniformly at random.",
    )
    parser.add_argument(
        "--render-mode",
        choices=["raw", "prefix_author"],
        default="raw",
        help="How to render text for embeddings/weak labels.",
    )
    parser.add_argument("--prefix-max-messages", type=int, default=5)
    parser.add_argument("--prefix-max-gap-seconds", type=int, default=180)
    parser.add_argument("--prefix-max-chars", type=int, default=4000)
    parser.add_argument("--prefix-boundary-regex")
    parser.add_argument("--context-before", type=int, default=2)
    parser.add_argument("--context-after", type=int, default=2)
    parser.add_argument("--quote-window", type=int, default=50)
    parser.add_argument("--max-content-length", type=int, default=0)
    parser.add_argument("--include-deleted", action="store_true")
    parser.add_argument("--include-empty", action="store_true")
    parser.add_argument("--channel-id", action="append", default=[])
    parser.add_argument("--annotator")
    parser.add_argument("--openai-key", help="OpenAI API key (or set OPENAI_API_KEY).")
    parser.add_argument(
        "--auto-annotator-model",
        default="gpt-5.2",
        help="OpenAI model for auto-annotation suggestions.",
    )
    parser.add_argument(
        "--auto-annotator-max-output-tokens",
        type=int,
        default=AUTO_ANNOTATOR_DEFAULT_MAX_OUTPUT_TOKENS,
        help="Max output tokens for auto-annotation responses.",
    )
    parser.add_argument(
        "--eval",
        action="store_true",
        help="Evaluate trained models on labeled data and exit.",
    )
    parser.add_argument(
        "--eval-threshold",
        type=float,
        default=0.5,
        help="Decision threshold for evaluation mode.",
    )
    parser.add_argument("--seed", type=int, default=13)
    parser.add_argument("--show-weak-labels", action="store_true")
    parser.add_argument(
        "--selection-strategy",
        choices=["uncertainty", "round_robin"],
        default="round_robin",
        help="How to select the next batch of messages to annotate.",
    )
    parser.add_argument(
        "--allow-mismatched-annotations",
        action="store_true",
        help="Use annotations even when content hashes/render specs do not match.",
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--color", action="store_true", help="Force ANSI colors (requires termcolor).")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI colors.")
    return parser.parse_args(argv)


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(message)s")


@contextmanager
def log_startup_step(step: str) -> Iterator[None]:
    start = time.perf_counter()
    try:
        yield
    finally:
        LOGGER.info("Startup: %s took %.3fs", step, time.perf_counter() - start)


@dataclass(frozen=True)
class TokenMarkovModel:
    transitions: dict[str, Counter[str]]
    totals: Counter[str]
    vocab_size: int

    def score(self, tokens: Sequence[str]) -> float:
        if not tokens:
            return 0.0
        total_log = 0.0
        transitions = 0
        prev = "<s>"
        for token in tokens:
            counts = self.transitions.get(prev)
            token_count = counts[token] if counts else 0
            total = self.totals.get(prev, 0)
            probability = (token_count + 1) / (total + self.vocab_size)
            total_log -= math.log(probability)
            transitions += 1
            prev = token
        return total_log / transitions


def _validate_label_name(name: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        raise ValueError(f"Invalid label name: {name}")
    return name


def _compile_cue_pattern(cue: str) -> re.Pattern[str]:
    text = cue.strip()
    if not text:
        raise ValueError("Cue must be a non-empty string.")
    if text.lower().startswith("re:"):
        pattern_text = text[3:].strip()
        if not pattern_text:
            raise ValueError("Regex cue must have content after 're:'.")
        try:
            return re.compile(pattern_text, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"Invalid regex cue: {cue!r} ({exc})") from exc
    parts = text.split()
    if not parts:
        raise ValueError("Cue must be a non-empty string.")
    escaped_parts = [re.escape(part) for part in parts]
    pattern = r"(?:^|\s)" + r"\s+".join(escaped_parts) + r"(?:$|\s)"
    return re.compile(pattern, re.IGNORECASE)


def _extract_leaf_groups(node: object) -> list[dict[str, object]]:
    if not isinstance(node, dict):
        return []
    subgroups = node.get("subgroups")
    if isinstance(subgroups, list) and subgroups:
        leaves: list[dict[str, object]] = []
        for child in subgroups:
            leaves.extend(_extract_leaf_groups(child))
        return leaves
    return [node]


def _matches_discord_invite(text: str) -> bool:
    return DISCORD_INVITE_RE.search(text) is not None


def _matches_file_link(pattern: re.Pattern[str], text: str) -> bool:
    return pattern.search(text) is not None


def _matches_linkdrop(text: str) -> bool:
    urls = URL_RE.findall(text)
    if not urls:
        return False
    if all(VIDEO_FILE_RE.search(url) for url in urls):
        return False
    remainder = URL_RE.sub("", text)
    remainder = re.sub(r"\s+", " ", remainder).strip()
    remainder = re.sub(r"[\W_]+", "", remainder)
    return len(remainder) < 15


def tokenize_text(text: str) -> list[str]:
    return TOKEN_RE.findall(text.lower())


def build_token_markov_model(texts: Sequence[str]) -> TokenMarkovModel:
    transitions: dict[str, Counter[str]] = {}
    totals: Counter[str] = Counter()
    vocab: set[str] = set()
    for text in texts:
        tokens = tokenize_text(text)
        if not tokens:
            continue
        prev = "<s>"
        for token in tokens:
            vocab.add(token)
            totals[prev] += 1
            transitions.setdefault(prev, Counter())[token] += 1
            prev = token
    vocab_size = max(1, len(vocab))
    return TokenMarkovModel(transitions=transitions, totals=totals, vocab_size=vocab_size)


def find_high_entropy_indices(
    texts: Sequence[str],
    *,
    percentile: float,
    min_tokens: int,
) -> set[int]:
    if not texts:
        return set()
    model = build_token_markov_model(texts)
    scored: list[tuple[float, int]] = []
    for idx, text in enumerate(texts):
        tokens = tokenize_text(text)
        if len(tokens) < min_tokens:
            continue
        score = model.score(tokens)
        scored.append((score, idx))
    if not scored:
        return set()
    scores_sorted = sorted(score for score, _ in scored)
    percentile = min(max(percentile, 0.0), 1.0)
    rank = int(math.floor(percentile * (len(scores_sorted) - 1)))
    threshold = scores_sorted[rank]
    return {idx for score, idx in scored if score >= threshold}


def _matcher_for_special(cue: str) -> Matcher | None:
    if cue == "~discord_invite":
        return _matches_discord_invite
    if cue == "~linkdrop":
        return _matches_linkdrop
    if cue == "~zip_file_link":
        return lambda text: _matches_file_link(ZIP_FILE_RE, text)
    if cue == "~exe_file_link":
        return lambda text: _matches_file_link(EXE_FILE_RE, text)
    if cue == "~apk_file_link":
        return lambda text: _matches_file_link(APK_FILE_RE, text)
    return None


def _build_label_definitions(items: Sequence[dict[str, object]]) -> list[LabelDefinition]:
    definitions: list[LabelDefinition] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Label entries must be objects.")
        raw_name = item.get("group") or item.get("name")
        name = _validate_label_name(coerce_str(raw_name, field="label.group", allow_empty=False))
        if name in seen:
            raise ValueError(f"Duplicate label name: {name}")
        seen.add(name)
        description = item.get("description")
        if description is None:
            description_text = ""
        elif isinstance(description, str):
            description_text = description
        else:
            description_text = str(description)
        raw_cues = item.get("cues", [])
        if raw_cues is None:
            cues: list[str] = []
        elif isinstance(raw_cues, list):
            cues = [str(cue) for cue in raw_cues if cue is not None]
        else:
            raise ValueError(f"cues for {name} must be a list")
        patterns: list[re.Pattern[str]] = []
        matchers: list[Matcher] = []
        special_cues: list[str] = []
        raw_examples = item.get("examples")
        if raw_examples is None:
            examples: dict[str, object] = {}
        elif isinstance(raw_examples, dict):
            examples = raw_examples
        else:
            raise ValueError(f"examples for {name} must be a mapping")
        positives = _coerce_example_list(examples.get("positives"), field=f"examples.positives for {name}")
        negatives = _coerce_example_list(examples.get("negatives"), field=f"examples.negatives for {name}")
        for cue in cues:
            text = cue.strip()
            if not text:
                continue
            if text.startswith("~"):
                special_cues.append(text)
                if text == "~high_entropy_text":
                    continue
                matcher = _matcher_for_special(text)
                if matcher is None:
                    LOGGER.warning("Unknown special cue %s for %s", text, name)
                else:
                    matchers.append(matcher)
                continue
            patterns.append(_compile_cue_pattern(text))
        definitions.append(
            LabelDefinition(
                name=name,
                description=description_text,
                patterns=tuple(patterns),
                matchers=tuple(matchers),
                special_cues=tuple(special_cues),
                positive_examples=positives,
                negative_examples=negatives,
            )
        )
    return definitions


def load_label_definitions(path: Path) -> list[LabelDefinition]:
    if not path.exists():
        raise FileNotFoundError(f"Label config not found: {path}")
    raw_text = path.read_text(encoding="utf-8")
    try:
        loaded = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ValueError(f"Failed to parse {path}: {exc}") from exc
    if not isinstance(loaded, list):
        raise ValueError("Label config must be a YAML list at the top level.")
    leaves: list[dict[str, object]] = []
    for entry in loaded:
        leaves.extend(_extract_leaf_groups(entry))
    if not leaves:
        raise ValueError("Label config did not yield any leaf labels.")
    return _build_label_definitions(leaves)


def _coerce_optional_str(value: object | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return str(value)


def _coerce_str_list(value: object | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        return [part.strip() for part in text.split(",") if part.strip()]
    raise ValueError("Expected list or comma-separated string for labels.")


def _coerce_example_list(value: object | None, *, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    examples: list[str] = []
    for item in value:
        if item is None:
            continue
        text = str(item).strip()
        if text:
            examples.append(text)
    return tuple(examples)


def parse_message(raw: dict[str, object], index: int) -> Message:
    message_id = coerce_str(raw.get("message_id"), field="message_id", allow_empty=False)
    channel_id = coerce_str(raw.get("channel_id"), field="channel_id", allow_empty=False)
    author_id = coerce_str(raw.get("author_id"), field="author_id", allow_empty=False)
    content_raw = raw.get("content")
    if content_raw is None:
        content = ""
    elif isinstance(content_raw, str):
        content = content_raw
    else:
        content = str(content_raw)
    created_at = _coerce_optional_str(raw.get("created_at"))
    deleted_at = _coerce_optional_str(raw.get("deleted_at"))
    mention_ids = coerce_optional_str_list(raw.get("mention_ids")) or []
    attachment_count = coerce_int(raw.get("attachment_count"), default=0)
    return Message(
        index=index,
        message_id=message_id,
        channel_id=channel_id,
        author_id=author_id,
        content=content,
        created_at=created_at,
        deleted_at=deleted_at,
        mention_ids=mention_ids,
        attachment_count=attachment_count,
    )


def load_messages(
    path: Path,
    *,
    include_deleted: bool,
    include_empty: bool,
    channel_ids: set[str] | None,
) -> list[Message]:
    messages: list[Message] = []
    with path.open("rt", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                raw = json.loads(text)
            except json.JSONDecodeError as exc:
                LOGGER.warning("Skipping line %s: %s", line_no, exc)
                continue
            if not isinstance(raw, dict):
                LOGGER.warning("Skipping line %s: expected object", line_no)
                continue
            try:
                message = parse_message(raw, len(messages))
            except ValueError as exc:
                LOGGER.warning("Skipping line %s: %s", line_no, exc)
                continue
            if channel_ids and message.channel_id not in channel_ids:
                continue
            if not include_deleted and message.deleted_at is not None:
                continue
            if not include_empty and not message.content.strip():
                continue
            messages.append(message)
    return messages


def normalize_text(text: str) -> str:
    cleaned = text.replace("\r", " ").replace("\n", " ")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip().lower()


def normalize_for_matching(text: str) -> str:
    cleaned = text.lower()
    cleaned = re.sub(r"[\W_]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def parse_epoch_seconds(created_at: str | None) -> float | None:
    if created_at is None:
        return None
    text = created_at.strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        cleaned = text
        if cleaned.endswith("Z"):
            cleaned = f"{cleaned[:-1]}+00:00"
        try:
            parsed = dt.datetime.fromisoformat(cleaned)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.timestamp()
    if value > 10**11:
        return value / 1000.0
    return value


def build_channel_index(messages: Sequence[Message]) -> tuple[dict[str, list[int]], dict[int, int]]:
    channel_to_indices: dict[str, list[int]] = {}
    index_to_pos: dict[int, int] = {}
    for idx, message in enumerate(messages):
        channel_indices = channel_to_indices.setdefault(message.channel_id, [])
        channel_indices.append(idx)
    for indices in channel_to_indices.values():
        for pos, idx in enumerate(indices):
            index_to_pos[idx] = pos
    return channel_to_indices, index_to_pos


def render_spec_payload(spec: RenderSpec) -> dict[str, object]:
    return {
        "mode": spec.mode,
        "prefix_max_messages": spec.prefix_max_messages,
        "prefix_max_gap_seconds": spec.prefix_max_gap_seconds,
        "prefix_max_chars": spec.prefix_max_chars,
        "boundary_regex": spec.boundary_regex,
    }


def render_spec_key(payload: dict[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=True)


def is_prefix_boundary(message: Message, *, boundary_re: re.Pattern[str] | None) -> bool:
    text = message.content.lstrip()
    if text.startswith(">"):
        return True
    if text.startswith("@") or text.startswith("<@"):
        return True
    if boundary_re and boundary_re.search(message.content):
        return True
    return False


def build_prefix_texts(
    messages: Sequence[Message],
    channel_index: dict[str, list[int]],
    *,
    max_messages: int,
    max_gap_seconds: int,
    max_chars: int,
    boundary_re: re.Pattern[str] | None,
    separator: str = PREFIX_SEPARATOR,
) -> list[str]:
    rendered = [""] * len(messages)
    timestamps = [parse_epoch_seconds(message.created_at) for message in messages]
    max_messages = max(1, max_messages)
    sep_len = len(separator)

    for indices in channel_index.values():
        run_author: str | None = None
        run_parts: deque[str] = deque()
        run_len = 0
        prev_idx: int | None = None

        for idx in indices:
            message = messages[idx]
            start_new = False
            if run_author != message.author_id:
                start_new = True
            elif prev_idx is not None:
                if is_prefix_boundary(message, boundary_re=boundary_re):
                    start_new = True
                else:
                    prev_ts = timestamps[prev_idx]
                    cur_ts = timestamps[idx]
                    if prev_ts is None or cur_ts is None:
                        start_new = True
                    elif (cur_ts - prev_ts) > max_gap_seconds:
                        start_new = True

            if start_new:
                run_author = message.author_id
                run_parts.clear()
                run_len = 0

            if run_parts:
                run_len += sep_len
            run_parts.append(message.content)
            run_len += len(message.content)

            while len(run_parts) > max_messages:
                removed = run_parts.popleft()
                run_len -= len(removed)
                if run_parts:
                    run_len -= sep_len

            while len(run_parts) > 1 and run_len > max_chars:
                removed = run_parts.popleft()
                run_len -= len(removed)
                if run_parts:
                    run_len -= sep_len

            rendered[idx] = separator.join(run_parts)
            prev_idx = idx

    return rendered


def compile_boundary_regex(pattern: str | None) -> re.Pattern[str] | None:
    if not pattern:
        return None
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"Invalid prefix boundary regex: {exc}") from exc


def compute_weak_labels(
    label_defs: Sequence[LabelDefinition],
    model_texts: Sequence[str],
    normalized_contents: Sequence[str],
    matching_contents: Sequence[str],
) -> dict[str, set[int]]:
    weak_labels: dict[str, set[int]] = {label.name: set() for label in label_defs}
    high_entropy_labels = [label.name for label in label_defs if "~high_entropy_text" in label.special_cues]
    if high_entropy_labels:
        indices = find_high_entropy_indices(
            model_texts,
            percentile=HIGH_ENTROPY_PERCENTILE,
            min_tokens=HIGH_ENTROPY_MIN_TOKENS,
        )
        for label_name in high_entropy_labels:
            weak_labels[label_name].update(indices)
    for idx, (content, matching, model_text) in enumerate(
        zip(normalized_contents, matching_contents, model_texts, strict=True)
    ):
        if not content and not model_text:
            continue
        for label in label_defs:
            matched = False
            if label.patterns and any(pattern.search(content) for pattern in label.patterns):
                matched = True
            elif matching != content and label.patterns and any(pattern.search(matching) for pattern in label.patterns):
                matched = True
            if matched:
                weak_labels[label.name].add(idx)
                continue
            if label.matchers and any(matcher(model_text) for matcher in label.matchers):
                weak_labels[label.name].add(idx)
    return weak_labels


def hash_content(content: str) -> str:
    hasher = hashlib.sha256()
    hasher.update(content.encode("utf-8"))
    return hasher.hexdigest()


def hash_texts(texts: Sequence[str]) -> str:
    hasher = hashlib.sha256()
    for text in texts:
        hasher.update(text.encode("utf-8"))
        hasher.update(b"\x1e")
    return hasher.hexdigest()


def build_embedding_cache_key(texts: Sequence[str], render_spec: RenderSpec) -> str:
    spec_payload = render_spec_payload(render_spec)
    spec_blob = render_spec_key(spec_payload)
    text_hash = hash_texts(texts)
    hasher = hashlib.sha256()
    hasher.update(spec_blob.encode("utf-8"))
    hasher.update(b"\x1e")
    hasher.update(text_hash.encode("utf-8"))
    return hasher.hexdigest()


def load_embedding_cache(cache_path: Path, *, expected_key: str, model_name: str) -> FloatArray | None:
    meta_path = cache_path.with_suffix(".meta.json")
    if not cache_path.exists() or not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(meta, dict):
        return None
    if meta.get("cache_key") != expected_key:
        return None
    if meta.get("model_name") != model_name:
        return None
    loaded = np.load(cache_path)
    return np.asarray(loaded, dtype=np.float32)


def save_embedding_cache(
    cache_path: Path,
    embeddings: FloatArray,
    *,
    cache_key: str,
    render_spec: RenderSpec,
    model_name: str,
) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    np.save(cache_path, embeddings)
    meta = {
        "cache_key": cache_key,
        "render_spec": render_spec_payload(render_spec),
        "model_name": model_name,
        "embedding_shape": list(embeddings.shape),
    }
    cache_path.with_suffix(".meta.json").write_text(
        json.dumps(meta, ensure_ascii=True, indent=2),
        encoding="utf-8",
    )


def compute_embeddings(
    texts: Sequence[str],
    *,
    model_name: str,
    cache_path: Path | None,
    render_spec: RenderSpec,
    embedder: SentenceTransformer | None = None,
) -> FloatArray:
    cache_key = build_embedding_cache_key(texts, render_spec)
    if cache_path is not None:
        cached = load_embedding_cache(cache_path, expected_key=cache_key, model_name=model_name)
        if cached is not None:
            LOGGER.info("Loaded cached embeddings from %s", cache_path)
            return cached
    LOGGER.info("Computing embeddings with %s", model_name)
    if embedder is None:
        embedder = SentenceTransformer(model_name)
    embeddings = embedder.encode(list(texts), show_progress_bar=False)
    embeddings_array = np.asarray(embeddings, dtype=np.float32)
    if cache_path is not None:
        save_embedding_cache(
            cache_path,
            embeddings_array,
            cache_key=cache_key,
            render_spec=render_spec,
            model_name=model_name,
        )
    return embeddings_array


def parse_annotation(raw: dict[str, object]) -> AnnotationRecord:
    message_id = coerce_str(raw.get("message_id"), field="message_id", allow_empty=False)
    channel_id = coerce_str(raw.get("channel_id"), field="channel_id", allow_empty=False)
    labels = _coerce_str_list(raw.get("labels"))
    label_names = _coerce_str_list(raw.get("label_names"))
    created_at = coerce_str(raw.get("created_at"), field="created_at", allow_empty=False)
    annotated_at = _coerce_optional_str(raw.get("annotated_at"))
    content_hash = coerce_str(raw.get("content_hash"), field="content_hash", allow_empty=False)
    annotator_raw = raw.get("annotator")
    annotator = annotator_raw if isinstance(annotator_raw, str) else _coerce_optional_str(annotator_raw)
    render_spec_raw = raw.get("render_spec")
    if render_spec_raw is None:
        render_spec = None
    elif isinstance(render_spec_raw, dict):
        render_spec = {str(key): value for key, value in render_spec_raw.items()}
    else:
        raise ValueError("render_spec must be an object when provided.")
    selection_strategy = _coerce_optional_str(raw.get("selection_strategy"))
    selection_reason = _coerce_optional_str(raw.get("selection_reason"))
    random_raw = raw.get("randomly_chosen")
    randomly_chosen = None if random_raw is None else coerce_bool(random_raw)
    return AnnotationRecord(
        message_id=message_id,
        channel_id=channel_id,
        labels=labels,
        label_names=label_names,
        created_at=created_at,
        annotated_at=annotated_at,
        content_hash=content_hash,
        annotator=annotator,
        render_spec=render_spec,
        selection_strategy=selection_strategy,
        selection_reason=selection_reason,
        randomly_chosen=randomly_chosen,
    )


def _with_annotation_updates(
    record: AnnotationRecord,
    *,
    annotated_at: str | None = None,
    content_hash: str | None = None,
    render_spec: dict[str, object] | None = None,
) -> AnnotationRecord:
    return AnnotationRecord(
        message_id=record.message_id,
        channel_id=record.channel_id,
        labels=record.labels,
        label_names=record.label_names,
        created_at=record.created_at,
        annotated_at=annotated_at if annotated_at is not None else record.annotated_at,
        content_hash=content_hash if content_hash is not None else record.content_hash,
        annotator=record.annotator,
        render_spec=render_spec if render_spec is not None else record.render_spec,
        selection_strategy=record.selection_strategy,
        selection_reason=record.selection_reason,
        randomly_chosen=record.randomly_chosen,
    )


def load_annotations(
    path: Path,
    messages: Sequence[Message],
    *,
    model_texts: Sequence[str],
    render_spec: RenderSpec,
    allow_mismatched: bool,
) -> dict[str, AnnotationRecord]:
    if not path.exists():
        return {}
    if len(model_texts) != len(messages):
        raise ValueError("Model texts must align with messages.")
    message_id_map = {message.message_id: message for message in messages}
    expected_render_spec = render_spec_payload(render_spec)
    expected_render_key = render_spec_key(expected_render_spec)
    expected_hashes = {message.message_id: hash_content(model_texts[idx]) for idx, message in enumerate(messages)}
    records: dict[str, AnnotationRecord] = {}
    legacy_count = 0
    default_annotated_at = dt.datetime.now(dt.timezone.utc).isoformat()
    with path.open("rt", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                raw = json.loads(text)
            except json.JSONDecodeError as exc:
                LOGGER.warning("Skipping annotation line %s: %s", line_no, exc)
                continue
            if not isinstance(raw, dict):
                LOGGER.warning("Skipping annotation line %s: expected object", line_no)
                continue
            try:
                record = parse_annotation(raw)
            except ValueError as exc:
                LOGGER.warning("Skipping annotation line %s: %s", line_no, exc)
                continue
            message = message_id_map.get(record.message_id)
            if message is None:
                LOGGER.warning("Annotation line %s references unknown message_id %s", line_no, record.message_id)
                continue
            annotated_at = record.annotated_at or default_annotated_at
            expected_hash = expected_hashes.get(record.message_id)
            if expected_hash is None:
                continue
            if record.render_spec is None:
                legacy_count += 1
                migrated = _with_annotation_updates(
                    record,
                    annotated_at=annotated_at,
                    content_hash=expected_hash,
                    render_spec=expected_render_spec,
                )
                records[record.message_id] = migrated
                continue
            record_with_time = _with_annotation_updates(record, annotated_at=annotated_at)
            record_render_key = render_spec_key(record.render_spec)
            if record_render_key != expected_render_key:
                if allow_mismatched:
                    LOGGER.warning("Annotation line %s render spec mismatch for %s", line_no, record.message_id)
                    records[record.message_id] = record_with_time
                else:
                    LOGGER.warning(
                        "Skipping annotation line %s: render spec mismatch for %s",
                        line_no,
                        record.message_id,
                    )
                continue
            if record.content_hash != expected_hash:
                if allow_mismatched:
                    LOGGER.warning("Annotation line %s content hash mismatch for %s", line_no, record.message_id)
                    records[record.message_id] = record_with_time
                else:
                    LOGGER.warning(
                        "Skipping annotation line %s: content hash mismatch for %s",
                        line_no,
                        record.message_id,
                    )
                continue
            records[record.message_id] = record_with_time
    if legacy_count:
        LOGGER.info("Migrated %s legacy annotations to render spec %s", legacy_count, render_spec.mode)
    return records


def append_annotation(path: Path, record: AnnotationRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "message_id": record.message_id,
        "channel_id": record.channel_id,
        "labels": record.labels,
        "label_names": record.label_names,
        "created_at": record.created_at,
        "annotated_at": record.annotated_at,
        "content_hash": record.content_hash,
        "annotator": record.annotator,
    }
    if record.render_spec is not None:
        payload["render_spec"] = record.render_spec
    if record.selection_strategy is not None:
        payload["selection_strategy"] = record.selection_strategy
    if record.selection_reason is not None:
        payload["selection_reason"] = record.selection_reason
    if record.randomly_chosen is not None:
        payload["randomly_chosen"] = record.randomly_chosen
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True) + "\n")


def build_training_data(
    label_name: str,
    annotations: dict[str, AnnotationRecord],
    message_id_to_index: dict[str, int],
    weak_labels: dict[str, set[int]],
    weak_label_weight: float,
    *,
    use_weak_labels: bool = True,
) -> tuple[IntArray, IntArray, FloatArray]:
    indices: list[int] = []
    labels: list[int] = []
    weights: list[float] = []
    annotated_indices: set[int] = set()

    if weak_label_weight < 0:
        raise ValueError("Weak label weight must be non-negative.")

    for record in annotations.values():
        idx = message_id_to_index.get(record.message_id)
        if idx is None:
            continue
        annotated_indices.add(idx)
        if label_name in record.labels:
            indices.append(idx)
            labels.append(1)
            weights.append(1.0)
            continue
        if label_name in record.label_names:
            indices.append(idx)
            labels.append(0)
            weights.append(1.0)

    if use_weak_labels and weak_label_weight > 0:
        weak_candidates = [idx for idx in weak_labels.get(label_name, set()) if idx not in annotated_indices]
        for idx in weak_candidates:
            indices.append(idx)
            labels.append(1)
            weights.append(float(weak_label_weight))

    return (
        np.asarray(indices, dtype=np.int64),
        np.asarray(labels, dtype=np.int64),
        np.asarray(weights, dtype=np.float32),
    )


def train_models(
    embeddings: FloatArray,
    label_defs: Sequence[LabelDefinition],
    annotations: dict[str, AnnotationRecord],
    message_id_to_index: dict[str, int],
    weak_labels: dict[str, set[int]],
    *,
    weak_label_weight: float,
    seed: int,
    embedder: SentenceTransformer | None = None,
    example_weight: float = LABEL_EXAMPLE_WEIGHT,
) -> dict[str, LogisticRegression]:
    models: dict[str, LogisticRegression] = {}
    start_time = time.perf_counter()
    for label in label_defs:
        indices, y_values, weights = build_training_data(
            label.name,
            annotations,
            message_id_to_index,
            weak_labels,
            weak_label_weight,
        )
        X_parts: list[FloatArray] = []
        y_parts: list[IntArray] = []
        w_parts: list[FloatArray] = []
        example_count = 0
        if indices.size:
            X_parts.append(embeddings[indices])
            y_parts.append(y_values)
            w_parts.append(weights)
        if embedder is not None and example_weight > 0:
            example_texts = list(label.positive_examples) + list(label.negative_examples)
            if example_texts:
                example_count = len(example_texts)
                example_labels = np.asarray(
                    [1] * len(label.positive_examples) + [0] * len(label.negative_examples),
                    dtype=np.int64,
                )
                example_embeddings = embedder.encode(example_texts, show_progress_bar=False)
                example_matrix = np.asarray(example_embeddings, dtype=np.float32)
                example_weights = np.full(example_labels.shape[0], example_weight, dtype=np.float32)
                X_parts.append(example_matrix)
                y_parts.append(example_labels)
                w_parts.append(example_weights)

        if not X_parts:
            LOGGER.info("Skipping %s: no training data", label.name)
            continue
        y_train = np.concatenate(y_parts)
        unique = set(int(value) for value in y_train)
        if len(unique) < 2:
            LOGGER.info("Skipping %s: need both positive and negative samples", label.name)
            continue
        X_train = np.vstack(X_parts)
        w_train = np.concatenate(w_parts)
        model = LogisticRegression(max_iter=1000, random_state=seed)
        model.fit(X_train, y_train, sample_weight=w_train)
        models[label.name] = model
        positives = int(np.sum(y_train == 1))
        negatives = int(np.sum(y_train == 0))
        if example_count:
            LOGGER.info(
                "Trained %s with %s positive and %s negative samples (%s label examples)",
                label.name,
                positives,
                negatives,
                example_count,
            )
        else:
            LOGGER.info("Trained %s with %s positive and %s negative samples", label.name, positives, negatives)
    elapsed = time.perf_counter() - start_time
    LOGGER.info("Model training completed in %.2fs.", elapsed)
    return models


def predict_probabilities(
    models: dict[str, LogisticRegression],
    embeddings: FloatArray,
) -> dict[str, FloatArray]:
    probabilities: dict[str, FloatArray] = {}
    for label_name, model in models.items():
        proba = np.asarray(model.predict_proba(embeddings), dtype=np.float32)
        if proba.ndim != 2 or proba.shape[1] < 2:
            LOGGER.warning("Skipping %s: unexpected predict_proba shape %s", label_name, proba.shape)
            continue
        probabilities[label_name] = proba[:, 1]
    return probabilities


def _safe_divide(numerator: float, denominator: float) -> float:
    if denominator == 0:
        return 0.0
    return numerator / denominator


def _compute_binary_metrics(tp: int, fp: int, fn: int, tn: int) -> tuple[float, float, float, float]:
    precision = _safe_divide(tp, tp + fp)
    recall = _safe_divide(tp, tp + fn)
    if precision + recall == 0:
        f1_score = 0.0
    else:
        f1_score = (2 * precision * recall) / (precision + recall)
    accuracy = _safe_divide(tp + tn, tp + tn + fp + fn)
    return precision, recall, f1_score, accuracy


def _build_eval_entries(
    annotations: dict[str, AnnotationRecord],
    message_id_to_index: dict[str, int],
) -> list[EvalEntry]:
    entries: list[EvalEntry] = []
    for record in annotations.values():
        idx = message_id_to_index.get(record.message_id)
        if idx is None:
            continue
        labels = frozenset(record.labels)
        label_names = frozenset(record.label_names)
        if not labels and not label_names:
            continue
        entries.append(EvalEntry(index=idx, labels=labels, label_names=label_names))
    return entries


def _evaluate_label_metrics(
    label_name: str,
    entries: Sequence[EvalEntry],
    probabilities: dict[str, FloatArray],
    *,
    threshold: float,
) -> EvalMetrics:
    series = probabilities.get(label_name)
    series_len = 0 if series is None else series.shape[0]
    tp = fp = fn = tn = 0
    support = 0
    positives = 0
    for entry in entries:
        if label_name in entry.labels:
            y_true = 1
        elif label_name in entry.label_names:
            y_true = 0
        else:
            continue
        if series is not None and entry.index >= series_len:
            LOGGER.warning("Skipping eval index %s for %s: out of range.", entry.index, label_name)
            continue
        support += 1
        if y_true == 1:
            positives += 1
        if series is None:
            continue
        score = float(series[entry.index])
        y_pred = 1 if score >= threshold else 0
        if y_pred == 1 and y_true == 1:
            tp += 1
        elif y_pred == 1 and y_true == 0:
            fp += 1
        elif y_pred == 0 and y_true == 1:
            fn += 1
        else:
            tn += 1
    if series is None:
        return EvalMetrics(
            label=label_name,
            support=support,
            positives=positives,
            tp=tp,
            fp=fp,
            fn=fn,
            tn=tn,
            precision=None,
            recall=None,
            f1=None,
            accuracy=None,
            status="no_model",
        )
    if support == 0:
        return EvalMetrics(
            label=label_name,
            support=0,
            positives=0,
            tp=0,
            fp=0,
            fn=0,
            tn=0,
            precision=None,
            recall=None,
            f1=None,
            accuracy=None,
            status="no_labels",
        )
    precision, recall, f1_score, accuracy = _compute_binary_metrics(tp=tp, fp=fp, fn=fn, tn=tn)
    return EvalMetrics(
        label=label_name,
        support=support,
        positives=positives,
        tp=tp,
        fp=fp,
        fn=fn,
        tn=tn,
        precision=precision,
        recall=recall,
        f1=f1_score,
        accuracy=accuracy,
        status="ok",
    )


def evaluate_models(
    label_defs: Sequence[LabelDefinition],
    annotations: dict[str, AnnotationRecord],
    message_id_to_index: dict[str, int],
    probabilities: dict[str, FloatArray],
    *,
    threshold: float,
) -> list[EvalMetrics]:
    entries = _build_eval_entries(annotations, message_id_to_index)
    if not entries:
        return []
    return [
        _evaluate_label_metrics(
            label.name,
            entries,
            probabilities,
            threshold=threshold,
        )
        for label in label_defs
    ]


def _format_optional_metric(value: float | None, *, precision: int = 3) -> str:
    if value is None:
        return "-"
    return f"{value:.{precision}f}"


def log_evaluation_results(results: Sequence[EvalMetrics], *, threshold: float) -> None:
    if not results:
        LOGGER.info("No evaluation results to report.")
        return
    label_width = max(5, max(len(result.label) for result in results))
    LOGGER.info("")
    LOGGER.info("Evaluation on labeled data (threshold=%.2f)", threshold)
    LOGGER.info(
        "%-*s  support  pos  precision  recall  f1  accuracy  status",
        label_width,
        "label",
    )
    for result in results:
        LOGGER.info(
            "%-*s  %7d  %3d  %9s  %6s  %4s  %8s  %s",
            label_width,
            result.label,
            result.support,
            result.positives,
            _format_optional_metric(result.precision),
            _format_optional_metric(result.recall),
            _format_optional_metric(result.f1),
            _format_optional_metric(result.accuracy),
            result.status,
        )
    total = len(results)
    with_data = sum(1 for result in results if result.status == "ok")
    no_model = sum(1 for result in results if result.status == "no_model")
    no_labels = sum(1 for result in results if result.status == "no_labels")
    LOGGER.info(
        "Evaluation summary: %s labels (%s with data, %s no model, %s no labels).",
        total,
        with_data,
        no_model,
        no_labels,
    )


def _select_uncertainty(
    unannotated_indices: Sequence[int],
    probabilities: dict[str, FloatArray],
    *,
    batch_size: int,
    rng: random.Random,
) -> list[int]:
    if not unannotated_indices:
        return []
    if not probabilities:
        shuffled = list(unannotated_indices)
        rng.shuffle(shuffled)
        return shuffled[:batch_size]
    scored: list[tuple[float, int]] = []
    for idx in unannotated_indices:
        margins = [abs(probabilities[label][idx] - 0.5) for label in probabilities]
        if not margins:
            continue
        scored.append((min(margins), idx))
    scored.sort(key=lambda item: item[0])
    return [idx for _, idx in scored[:batch_size]]


def _select_round_robin(
    unannotated_indices: Sequence[int],
    label_defs: Sequence[LabelDefinition],
    probabilities: dict[str, FloatArray],
    weak_labels: dict[str, set[int]],
    *,
    batch_size: int,
    rng: random.Random,
) -> list[int]:
    ordered_unannotated = list(unannotated_indices)
    available = set(ordered_unannotated)
    if not available:
        return []
    available_indices = np.asarray(ordered_unannotated, dtype=np.int64)
    shortlist_size = min(
        available_indices.size,
        max(batch_size * ROUND_ROBIN_SHORTLIST_MULTIPLIER, batch_size),
    )
    label_queues: dict[str, deque[int]] = {}
    for label in label_defs:
        name = label.name
        if name in probabilities:
            series = probabilities[name]
            margins = np.abs(series[available_indices] - 0.5)
            if margins.size <= shortlist_size:
                order = np.argsort(margins)
            else:
                shortlist = np.argpartition(margins, shortlist_size - 1)[:shortlist_size]
                order = shortlist[np.argsort(margins[shortlist])]
            queue = deque(int(available_indices[idx]) for idx in order)
        else:
            weak = [idx for idx in weak_labels.get(name, set()) if idx in available]
            weak.sort()
            rng.shuffle(weak)
            queue = deque(weak)
        if queue:
            label_queues[name] = queue

    selected: list[int] = []
    while len(selected) < batch_size and label_queues:
        progress = False
        for label in label_defs:
            name = label.name
            queue_for_label = label_queues.get(name)
            if queue_for_label is None:
                continue
            if not queue_for_label:
                continue
            while queue_for_label and queue_for_label[0] not in available:
                queue_for_label.popleft()
            if not queue_for_label:
                label_queues.pop(name, None)
                continue
            idx = queue_for_label.popleft()
            selected.append(idx)
            available.remove(idx)
            progress = True
            if len(selected) >= batch_size:
                break
        if not progress:
            break
    if len(selected) < batch_size and available:
        remaining = sorted(available)
        rng.shuffle(remaining)
        selected.extend(remaining[: batch_size - len(selected)])
    return selected


def select_candidates(
    messages: Sequence[Message],
    label_defs: Sequence[LabelDefinition],
    probabilities: dict[str, FloatArray],
    annotations: dict[str, AnnotationRecord],
    weak_labels: dict[str, set[int]],
    *,
    batch_size: int,
    rng: random.Random,
    strategy: str,
    random_sample_fraction: float,
) -> list[SelectedCandidate]:
    annotated_ids = set(annotations.keys())
    unannotated_indices = [idx for idx, message in enumerate(messages) if message.message_id not in annotated_ids]
    if not unannotated_indices:
        return []
    if not 0 <= random_sample_fraction <= 1:
        raise ValueError("random_sample_fraction must be between 0 and 1.")
    random_count = int(batch_size * random_sample_fraction)
    if random_sample_fraction > 0 and random_count == 0 and batch_size > 0:
        random_count = 1
    random_count = min(random_count, batch_size, len(unannotated_indices))
    random_indices: list[int] = []
    if random_count:
        random_pool = list(unannotated_indices)
        rng.shuffle(random_pool)
        random_indices = random_pool[:random_count]
    random_set = set(random_indices)
    remaining_indices = [idx for idx in unannotated_indices if idx not in random_set]
    active_count = batch_size - len(random_indices)
    if active_count <= 0 or not remaining_indices:
        active_indices: list[int] = []
    elif strategy == "round_robin":
        active_indices = _select_round_robin(
            remaining_indices,
            label_defs,
            probabilities,
            weak_labels,
            batch_size=active_count,
            rng=rng,
        )
    else:
        active_indices = _select_uncertainty(
            remaining_indices,
            probabilities,
            batch_size=active_count,
            rng=rng,
        )
    selected: list[SelectedCandidate] = [
        SelectedCandidate(
            index=idx,
            selection_strategy=strategy,
            selection_reason="active_learning",
            randomly_chosen=False,
        )
        for idx in active_indices
    ]
    selected.extend(
        SelectedCandidate(
            index=idx,
            selection_strategy="random",
            selection_reason="randomly_chosen",
            randomly_chosen=True,
        )
        for idx in random_indices
    )
    return selected


def extract_quote_snippets(content: str) -> list[str]:
    snippets: list[str] = []
    for line in content.splitlines():
        stripped = line.lstrip()
        if not stripped.startswith(">"):
            continue
        snippet = stripped.lstrip(">").strip()
        if snippet:
            snippets.append(snippet)
    return snippets


def find_previous_by_author(
    channel_indices: Sequence[int],
    start_pos: int,
    messages: Sequence[Message],
    author_id: str,
) -> int | None:
    for pos in range(start_pos - 1, -1, -1):
        idx = channel_indices[pos]
        if messages[idx].author_id == author_id:
            return idx
    return None


def find_quote_reference(
    channel_indices: Sequence[int],
    start_pos: int,
    normalized_contents: Sequence[str],
    snippet: str,
    *,
    search_window: int,
) -> int | None:
    if not snippet:
        return None
    target = normalize_text(snippet)
    if not target:
        return None
    start = max(0, start_pos - search_window)
    for pos in range(start_pos - 1, start - 1, -1):
        idx = channel_indices[pos]
        if target in normalized_contents[idx]:
            return idx
    return None


def build_context_indices(
    target_idx: int,
    messages: Sequence[Message],
    channel_index: dict[str, list[int]],
    index_to_pos: dict[int, int],
    normalized_contents: Sequence[str],
    *,
    context_before: int,
    context_after: int,
    quote_window: int,
) -> tuple[list[int], set[int]]:
    target = messages[target_idx]
    channel_indices = channel_index[target.channel_id]
    pos = index_to_pos[target_idx]
    start = max(0, pos - context_before)
    end = min(len(channel_indices) - 1, pos + context_after)
    context_set = set(channel_indices[start : end + 1])
    referenced: set[int] = set()

    for mention in target.mention_ids:
        ref_idx = find_previous_by_author(channel_indices, pos, messages, mention)
        if ref_idx is not None:
            referenced.add(ref_idx)
            context_set.add(ref_idx)

    for snippet in extract_quote_snippets(target.content):
        ref_idx = find_quote_reference(
            channel_indices,
            pos,
            normalized_contents,
            snippet,
            search_window=quote_window,
        )
        if ref_idx is not None:
            referenced.add(ref_idx)
            context_set.add(ref_idx)

    ordered = sorted(context_set, key=lambda idx: index_to_pos[idx])
    return ordered, referenced


def format_content(
    content: str,
    *,
    max_length: int | None,
    highlighter: Callable[[str], str] | None = None,
) -> str:
    rendered = content.replace("\r", "")
    if max_length is not None and max_length > 0 and len(rendered) > max_length:
        rendered = f"{rendered[: max_length - 3]}..."
    if highlighter is not None:
        rendered = highlighter(rendered)
    rendered = rendered.replace("\n", "\\n")
    return rendered


def format_created_at(created_at: str | None) -> str | None:
    seconds = parse_epoch_seconds(created_at)
    if seconds is None or seconds <= 0:
        return None
    local_time = dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc).astimezone(NY_TZ)
    return local_time.strftime("%Y-%m-%d %H:%M:%S %Z")


def _trim_span(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _find_pattern_spans(pattern: re.Pattern[str], text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for match in pattern.finditer(text):
        start, end = _trim_span(text, match.start(), match.end())
        if start >= end:
            continue
        spans.append((start, end))
    return spans


def _build_loose_pattern_from_match(match_text: str) -> re.Pattern[str] | None:
    tokens = [token for token in re.split(r"\s+", match_text.strip()) if token]
    if not tokens:
        return None
    if len(tokens) == 1:
        return re.compile(re.escape(tokens[0]), re.IGNORECASE)
    pattern = r"(?:^|\W)" + r"\W+".join(re.escape(token) for token in tokens) + r"(?:$|\W)"
    return re.compile(pattern, re.IGNORECASE)


def _collect_weak_label_spans(
    text: str,
    normalized_text: str,
    matching_text: str,
    labels: Sequence[LabelDefinition],
) -> dict[str, set[tuple[int, int]]]:
    spans_by_label: dict[str, set[tuple[int, int]]] = {}
    for label in labels:
        spans: set[tuple[int, int]] = set()
        for pattern in label.patterns:
            spans.update(_find_pattern_spans(pattern, text))
            if matching_text != normalized_text:
                for match in pattern.finditer(matching_text):
                    loose_pattern = _build_loose_pattern_from_match(match.group(0))
                    if loose_pattern is None:
                        continue
                    spans.update(_find_pattern_spans(loose_pattern, text))
        for cue in label.special_cues:
            cue_pattern = WEAK_CUE_HIGHLIGHT_PATTERNS.get(cue)
            if cue_pattern is None:
                continue
            spans.update(_find_pattern_spans(cue_pattern, text))
        if spans:
            spans_by_label[label.name] = spans
    return spans_by_label


def _build_weak_highlight_spans(spans_by_label: dict[str, set[tuple[int, int]]]) -> list[_HighlightSpan]:
    combined: set[tuple[int, int]] = set()
    for spans in spans_by_label.values():
        combined.update(spans)
    return [
        _HighlightSpan(
            start=start,
            end=end,
            color=WEAK_HIGHLIGHT_COLOR,
            on_color=WEAK_HIGHLIGHT_ON_COLOR,
            attrs=WEAK_HIGHLIGHT_ATTRS,
            priority=WEAK_HIGHLIGHT_PRIORITY,
        )
        for start, end in sorted(combined)
    ]


def display_context(
    target_idx: int,
    messages: Sequence[Message],
    channel_index: dict[str, list[int]],
    index_to_pos: dict[int, int],
    normalized_contents: Sequence[str],
    *,
    context_before: int,
    context_after: int,
    quote_window: int,
    max_content_length: int | None,
    referenced_labels: Iterable[str] | None = None,
    weak_label_defs: Sequence[LabelDefinition] | None = None,
) -> None:
    ordered, referenced = build_context_indices(
        target_idx,
        messages,
        channel_index,
        index_to_pos,
        normalized_contents,
        context_before=context_before,
        context_after=context_after,
        quote_window=quote_window,
    )
    weak_highlights: dict[int, list[_HighlightSpan]] = {}
    found_labels: set[str] = set()
    if weak_label_defs:
        for idx in ordered:
            message = messages[idx]
            raw_content = message.content.replace("\r", "")
            normalized = normalized_contents[idx]
            matching = normalize_for_matching(raw_content)
            spans_by_label = _collect_weak_label_spans(
                raw_content,
                normalized,
                matching,
                weak_label_defs,
            )
            if spans_by_label:
                found_labels.update(spans_by_label)
                weak_highlights[idx] = _build_weak_highlight_spans(spans_by_label)
    target = messages[target_idx]
    LOGGER.info("")
    LOGGER.info("Context for %s (channel %s)", target.message_id, target.channel_id)
    LOGGER.info("----------------------------------------")
    for idx in ordered:
        message = messages[idx]
        marker = "->" if idx in referenced else "  "
        if idx == target_idx:
            marker = ">>"
        if marker == ">>":
            marker_colored = _c(marker, color="white", on_color="on_blue", attrs=("bold",))
        elif marker == "->":
            marker_colored = _c(marker, color="yellow", attrs=("bold",))
        else:
            marker_colored = _c(marker, color="white", attrs=("dark",))
        if idx == target_idx:
            author_txt = _c(message.author_id, color="cyan", attrs=("bold",))
            msgid_txt = _c(message.message_id, color="blue", attrs=("bold",))
        else:
            author_txt = _c(message.author_id, color="cyan")
            msgid_txt = _c(message.message_id, color="blue")
        header = f"{author_txt} {msgid_txt}"
        timestamp = format_created_at(message.created_at)
        if timestamp:
            header = f"{header} {_c(timestamp, color='magenta', attrs=('dark',))}"
        extra_spans = weak_highlights.get(idx)

        def highlighter(
            text: str,
            spans: Sequence[_HighlightSpan] | None = extra_spans,
        ) -> str:
            return highlight_text_with_spans(text, spans)

        content = format_content(message.content, max_length=max_content_length, highlighter=highlighter)
        LOGGER.info("%s %s: %s", marker_colored, header, content)
    if referenced_labels:
        labels_text = ", ".join(_c(name, color="magenta", attrs=("bold",)) for name in sorted(referenced_labels))
        LOGGER.info("%s %s", _c("Weak label matches:", color="magenta", attrs=("bold",)), labels_text)
    if weak_label_defs:
        missing_labels = {label.name for label in weak_label_defs if label.name not in found_labels}
        if missing_labels:
            LOGGER.info(
                "%s %s",
                _c("Weak label triggers not found:", color="magenta", attrs=("bold",)),
                ", ".join(sorted(missing_labels)),
            )
    LOGGER.info("----------------------------------------")


def _resolve_openai_key(cli_key: str | None) -> str | None:
    if cli_key is not None:
        value = cli_key.strip()
        if value:
            return value
        return None
    env_key = os.environ.get("OPENAI_API_KEY")
    if env_key is None:
        return None
    env_value = env_key.strip()
    return env_value or None


def _build_openai_client(api_key: str) -> OpenAI:
    import openai

    return openai.OpenAI(api_key=api_key)


def _build_auto_annotator_schema(label_names: Sequence[str]) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "labels": {
                "type": "array",
                "items": {"type": "string", "enum": list(label_names)},
            }
        },
        "required": ["labels"],
    }


def _build_auto_annotator_payload(
    target_idx: int,
    messages: Sequence[Message],
    channel_index: dict[str, list[int]],
    index_to_pos: dict[int, int],
    normalized_contents: Sequence[str],
    *,
    context_before: int,
    context_after: int,
    quote_window: int,
    max_content_length: int | None,
    suggestions: Sequence[tuple[str, float]],
    label_defs: Sequence[LabelDefinition],
) -> dict[str, object]:
    ordered, referenced = build_context_indices(
        target_idx,
        messages,
        channel_index,
        index_to_pos,
        normalized_contents,
        context_before=context_before,
        context_after=context_after,
        quote_window=quote_window,
    )
    context_messages: list[dict[str, object]] = []
    reply_to: list[dict[str, object]] = []
    for idx in ordered:
        message = messages[idx]
        timestamp = format_created_at(message.created_at)
        content = format_content(message.content, max_length=max_content_length, highlighter=None)
        entry: dict[str, object] = {
            "message_id": message.message_id,
            "channel_id": message.channel_id,
            "author_id": message.author_id,
            "created_at": message.created_at,
            "display_created_at": timestamp,
            "content": content,
            "is_target": idx == target_idx,
            "is_reply_to": idx in referenced,
        }
        context_messages.append(entry)
        if idx in referenced:
            reply_to.append(entry)
    target = messages[target_idx]
    target_payload = {
        "message_id": target.message_id,
        "channel_id": target.channel_id,
        "author_id": target.author_id,
        "created_at": target.created_at,
        "display_created_at": format_created_at(target.created_at),
        "content": format_content(target.content, max_length=max_content_length, highlighter=None),
    }
    likely_labels = [{"label": name, "score": score} for name, score in suggestions]
    label_payload = [
        {"label": label.name, "description": label.description or "(no description)"} for label in label_defs
    ]
    return {
        "target_message": target_payload,
        "context_messages": context_messages,
        "reply_to_messages": reply_to,
        "most_likely_labels": likely_labels,
        "labels": label_payload,
    }


def _extract_response_text(response: Response) -> str:
    if response.error is not None:
        raise ValueError(f"Auto-annotator error: {response.error.code} {response.error.message}")
    chunks: list[str] = []
    for item in response.output:
        if getattr(item, "type", None) != "message":
            continue
        content_items = getattr(item, "content", None)
        if not isinstance(content_items, list):
            continue
        for content in content_items:
            if getattr(content, "type", None) != "output_text":
                continue
            text = getattr(content, "text", None)
            if isinstance(text, str):
                chunks.append(text)
    text = "".join(chunks).strip()
    if not text:
        raise ValueError("Auto-annotator response missing text.")
    return text


def _parse_auto_annotator_labels(text: str, label_names: set[str]) -> list[str]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError("Auto-annotator response was not valid JSON.") from exc
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc2:
            raise ValueError("Auto-annotator response was not valid JSON.") from exc2
    if not isinstance(payload, dict):
        raise ValueError("Auto-annotator response must be a JSON object.")
    raw_labels = payload.get("labels")
    if not isinstance(raw_labels, list):
        raise ValueError("Auto-annotator response missing labels.")
    labels: list[str] = []
    unknown: list[str] = []
    for item in raw_labels:
        if not isinstance(item, str):
            raise ValueError("Auto-annotator labels must be strings.")
        name = item.strip()
        if not name:
            continue
        if name not in label_names:
            unknown.append(name)
            continue
        labels.append(name)
    if unknown:
        raise ValueError(f"Auto-annotator returned unknown labels: {', '.join(unknown)}")
    return sorted(set(labels))


def _auto_annotate_labels(
    annotator: AutoAnnotator,
    *,
    target_idx: int,
    messages: Sequence[Message],
    channel_index: dict[str, list[int]],
    index_to_pos: dict[int, int],
    normalized_contents: Sequence[str],
    context_before: int,
    context_after: int,
    quote_window: int,
    max_content_length: int | None,
    suggestions: Sequence[tuple[str, float]],
    label_defs: Sequence[LabelDefinition],
) -> list[str]:
    payload = _build_auto_annotator_payload(
        target_idx,
        messages,
        channel_index,
        index_to_pos,
        normalized_contents,
        context_before=context_before,
        context_after=context_after,
        quote_window=quote_window,
        max_content_length=max_content_length,
        suggestions=suggestions,
        label_defs=label_defs,
    )
    system_prompt = (
        "You are an auto-annotator for message labels. "
        "Use only the provided labels and descriptions. "
        "Input JSON includes target_message, context_messages, reply_to_messages, most_likely_labels, and labels. "
        "The most_likely_labels list is a suggestion, not a requirement. "
        "Multi-label output is allowed. "
        "Return JSON that matches the schema. "
        "If no labels apply, return an empty list."
    )
    response = annotator.client.responses.create(
        model=annotator.model,
        instructions=system_prompt,
        input=json.dumps(payload, ensure_ascii=True),
        text={
            "format": {
                "type": "json_schema",
                "name": "annotation_labels",
                "schema": annotator.schema,
                "strict": True,
            }
        },
        temperature=0,
        max_output_tokens=annotator.max_output_tokens,
    )
    text = _extract_response_text(response)
    label_set = set(annotator.label_names)
    return _parse_auto_annotator_labels(text, label_set)


def _build_auto_annotator(
    label_defs: Sequence[LabelDefinition],
    *,
    api_key: str | None,
    model: str,
    max_output_tokens: int,
) -> AutoAnnotator | None:
    if api_key is None:
        return None
    label_names = tuple(label.name for label in label_defs)
    schema = _build_auto_annotator_schema(label_names)
    client = _build_openai_client(api_key)
    return AutoAnnotator(
        client=client,
        model=model,
        max_output_tokens=max_output_tokens,
        schema=schema,
        label_names=label_names,
    )


def log_label_help(label_defs: Sequence[LabelDefinition]) -> None:
    LOGGER.info("Labels (multi-label allowed, likely exclusive):")
    for idx, label in enumerate(label_defs, start=1):
        description = label.description or "(no description)"
        LOGGER.info("  %s) %s - %s", idx, label.name, description)


def log_label_summary(label_defs: Sequence[LabelDefinition]) -> None:
    LOGGER.info(
        "Loaded %s labels. Use '?' to list all labels. Tab autocompletes label names.",
        len(label_defs),
    )


def _setup_autocomplete(options: Sequence[str]) -> Callable[[], None]:
    if not sys.stdin.isatty():
        global _AUTOCOMPLETE_WARNED
        if not _AUTOCOMPLETE_WARNED:
            LOGGER.info("Autocomplete unavailable (stdin is not a TTY).")
            _AUTOCOMPLETE_WARNED = True
        return lambda: None
    try:
        import readline
    except ImportError:
        if not _AUTOCOMPLETE_WARNED:
            LOGGER.info("Autocomplete unavailable (readline not installed).")
            _AUTOCOMPLETE_WARNED = True
        return lambda: None

    original_completer = readline.get_completer()
    original_delims = readline.get_completer_delims()

    def completer(text: str, state: int) -> str | None:
        buffer = readline.get_line_buffer()
        if "," in buffer:
            start = buffer.rfind(",") + 1
            token = buffer[start:].lstrip()
        else:
            token = buffer.strip()
        if not token:
            matches = options
        else:
            matches = [opt for opt in options if opt.lower().startswith(token.lower())]
        if state < len(matches):
            return matches[state]
        return None

    readline.set_completer(completer)
    readline.set_completer_delims(" ,")
    if "libedit" in (readline.__doc__ or "").lower():
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind("tab: complete")

    def restore() -> None:
        readline.set_completer(original_completer)
        readline.set_completer_delims(original_delims)

    return restore


def build_autocomplete_options(label_defs: Sequence[LabelDefinition]) -> list[str]:
    options = [label.name for label in label_defs]
    options.extend(str(idx) for idx in range(1, len(label_defs) + 1))
    return options


def suggest_label_scores(
    probabilities: dict[str, FloatArray],
    label_defs: Sequence[LabelDefinition],
    idx: int,
    *,
    limit: int,
) -> list[tuple[str, float]]:
    scored: list[tuple[str, float]] = []
    for label in label_defs:
        series = probabilities.get(label.name)
        if series is None:
            continue
        scored.append((label.name, float(series[idx])))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored[:limit]


def build_suggestions(
    probabilities: dict[str, FloatArray],
    label_defs: Sequence[LabelDefinition],
    idx: int,
    weak_matches: Sequence[str],
    *,
    limit: int,
) -> list[tuple[str, float]]:
    if probabilities:
        return suggest_label_scores(probabilities, label_defs, idx, limit=limit)
    if weak_matches:
        return [(name, 1.0) for name in sorted(set(weak_matches))][:limit]
    return []


def _format_suggestions(suggestions: Sequence[tuple[str, float]]) -> list[str]:
    lines: list[str] = []
    for name, score in suggestions:
        bar = _prob_bar(score)
        color = _prob_color(score)
        lines.append(
            f"{_c(name, color=color, attrs=('bold',))} "
            f"{_c(bar, color=color)} "
            f"{_c(f'{score:.2f}', color=color, attrs=('dark',))}"
        )
    return lines


def prompt_for_labels(
    label_defs: Sequence[LabelDefinition],
    *,
    suggestions: Sequence[tuple[str, float]] | None,
    weak_matches: Sequence[str] | None,
    autocomplete_options: Sequence[str],
    auto_labels: Sequence[str] | None,
) -> list[str] | None:
    global _SUGGESTION_WARNED
    label_names = [label.name for label in label_defs]
    index_map = {str(idx): name for idx, name in enumerate(label_names, start=1)}
    name_map = {name.lower(): name for name in label_names}
    if suggestions:
        LOGGER.info("%s", _c("Top labels:", color="white", attrs=("bold",)))
        for line in _format_suggestions(suggestions):
            LOGGER.info("  %s", line)
    elif not _SUGGESTION_WARNED:
        LOGGER.info("No model suggestions yet. Use '?' to list labels.")
        _SUGGESTION_WARNED = True
    if weak_matches:
        labels_text = ", ".join(_c(name, color="magenta", attrs=("bold",)) for name in sorted(set(weak_matches)))
        LOGGER.info("%s %s", _c("Weak matches:", color="magenta", attrs=("bold",)), labels_text)
    if auto_labels is not None:
        auto_text = ", ".join(auto_labels) if auto_labels else "(none)"
        LOGGER.info("%s %s", _c("Auto-annotator:", color="cyan", attrs=("bold",)), auto_text)
    while True:
        restore = _setup_autocomplete(autocomplete_options)
        try:
            options = "comma-separated names or numbers, blank=none, s=skip, q=quit, ?=help"
            if auto_labels is not None:
                options = f"{options}, a=accept-auto"
            prompt = _c("labels", color="cyan", attrs=("bold",)) + f" ({options}): "
            response = input(prompt).strip()
        finally:
            restore()
        if not response:
            return []
        lowered = response.lower()
        if lowered in {"q", "quit"}:
            raise QuitAnnotation
        if lowered in {"s", "skip"}:
            return None
        if lowered in {"a", "auto", "accept"}:
            if auto_labels is None:
                LOGGER.info("Auto-annotator unavailable (set --openai-key or OPENAI_API_KEY).")
                continue
            return sorted(set(auto_labels))
        if lowered in {"?", "help"}:
            log_label_help(label_defs)
            continue
        tokens = re.split(r"[\s,]+", response)
        selections: list[str] = []
        invalid: list[str] = []
        for token in tokens:
            if not token:
                continue
            if token in index_map:
                selections.append(index_map[token])
                continue
            name = name_map.get(token.lower())
            if name is None:
                invalid.append(token)
                continue
            selections.append(name)
        if invalid:
            for token in invalid:
                matches = [name for name in label_names if name.lower().startswith(token.lower())][:8]
                if matches:
                    LOGGER.info("Matches for '%s': %s", token, ", ".join(matches))
            LOGGER.info("Unknown labels: %s", ", ".join(invalid))
            continue
        return sorted(set(selections))


def ensure_context_bounds(
    context_before: int,
    context_after: int,
    *,
    min_before: int = 0,
) -> tuple[int, int]:
    before = max(context_before, min_before, 0)
    after = max(context_after, 0)
    if before != context_before or after != context_after:
        LOGGER.info("Adjusted context window to before=%s after=%s", before, after)
    return before, after


def main(argv: Sequence[str]) -> int:
    parse_start = time.perf_counter()
    args = parse_args(argv)
    parse_elapsed = time.perf_counter() - parse_start

    with log_startup_step("configure logging"):
        configure_logging(args.verbose)
    LOGGER.info("Startup: parsed args in %.3fs", parse_elapsed)

    with log_startup_step("configure color output"):
        if args.color and args.no_color:
            LOGGER.error("Cannot use --color and --no-color together.")
            return 2
        force_color: bool | None = None
        if args.no_color:
            force_color = False
        elif args.color:
            force_color = True
        configure_color_output(force=force_color)
        if args.color and _term_colored is None:
            LOGGER.warning("termcolor is not installed; continuing without color.")

    with log_startup_step("validate arguments"):
        if args.batch_size <= 0:
            LOGGER.error("Batch size must be positive.")
            return 2
        if args.weak_label_weight < 0:
            LOGGER.error("Weak label weight must be non-negative.")
            return 2
        if args.prefix_max_messages <= 0:
            LOGGER.error("Prefix max messages must be positive.")
            return 2
        if args.prefix_max_gap_seconds < 0:
            LOGGER.error("Prefix max gap seconds must be non-negative.")
            return 2
        if args.prefix_max_chars < 0:
            LOGGER.error("Prefix max chars must be non-negative.")
            return 2
        if not 0 <= args.random_sample_fraction <= 1:
            LOGGER.error("Random sample fraction must be between 0 and 1.")
            return 2
        if not 0 <= args.eval_threshold <= 1:
            LOGGER.error("Eval threshold must be between 0 and 1.")
            return 2
        if args.retrain_interval < 0:
            LOGGER.error("Retrain interval must be non-negative.")
            return 2
        if args.openai_key is not None and not args.openai_key.strip():
            LOGGER.error("OpenAI key cannot be empty.")
            return 2
        if not args.auto_annotator_model.strip():
            LOGGER.error("Auto-annotator model must be non-empty.")
            return 2
        if args.auto_annotator_max_output_tokens <= 0:
            LOGGER.error("Auto-annotator max output tokens must be positive.")
            return 2

    channel_ids = {str(value) for value in args.channel_id if value}

    with log_startup_step("load label definitions"):
        label_defs = load_label_definitions(args.label_config)
        log_label_summary(label_defs)
    with log_startup_step("initialize auto-annotator"):
        api_key = _resolve_openai_key(args.openai_key)
        auto_annotator = _build_auto_annotator(
            label_defs,
            api_key=api_key,
            model=args.auto_annotator_model,
            max_output_tokens=args.auto_annotator_max_output_tokens,
        )
    if auto_annotator is not None:
        LOGGER.info("Auto-annotator enabled. Press 'a' to accept its labels.")

    with log_startup_step("load messages"):
        messages = load_messages(
            args.input,
            include_deleted=args.include_deleted,
            include_empty=args.include_empty,
            channel_ids=channel_ids or None,
        )
    if not messages:
        LOGGER.error("No messages loaded from %s", args.input)
        return 1

    with log_startup_step("build channel index"):
        channel_index, index_to_pos = build_channel_index(messages)
    with log_startup_step("build render spec"):
        render_spec = (
            RenderSpec(
                mode="prefix_author",
                prefix_max_messages=args.prefix_max_messages,
                prefix_max_gap_seconds=args.prefix_max_gap_seconds,
                prefix_max_chars=args.prefix_max_chars,
                boundary_regex=args.prefix_boundary_regex,
            )
            if args.render_mode == "prefix_author"
            else RenderSpec(
                mode="raw",
                prefix_max_messages=None,
                prefix_max_gap_seconds=None,
                prefix_max_chars=None,
                boundary_regex=None,
            )
        )
        try:
            boundary_re = compile_boundary_regex(render_spec.boundary_regex)
        except ValueError as exc:
            LOGGER.error("%s", exc)
            return 2
    with log_startup_step("build model texts"):
        if render_spec.mode == "prefix_author":
            model_texts = build_prefix_texts(
                messages,
                channel_index,
                max_messages=args.prefix_max_messages,
                max_gap_seconds=args.prefix_max_gap_seconds,
                max_chars=args.prefix_max_chars,
                boundary_re=boundary_re,
            )
        else:
            model_texts = [message.content for message in messages]
    with log_startup_step("normalize message contents"):
        normalized_contents = [normalize_text(message.content) for message in messages]

    with log_startup_step("load annotations"):
        annotations = load_annotations(
            args.annotations,
            messages,
            model_texts=model_texts,
            render_spec=render_spec,
            allow_mismatched=args.allow_mismatched_annotations,
        )
    annotated_count = len(annotations)
    LOGGER.info("Loaded %s messages (%s annotated)", len(messages), annotated_count)

    cache_path = None if args.no_embedding_cache else args.embedding_cache
    weak_labels: dict[str, set[int]] = {}
    with log_startup_step("compute weak labels"):
        if args.weak_label_weight <= 0:
            weak_labels = {label.name: set() for label in label_defs}
            if args.show_weak_labels:
                LOGGER.info("Weak labels disabled because --weak-label-weight=0; --show-weak-labels has no effect.")
        else:
            normalized_model_texts = [normalize_text(text) for text in model_texts]
            matching_model_texts = [normalize_for_matching(text) for text in model_texts]
            weak_labels = compute_weak_labels(
                label_defs,
                model_texts,
                normalized_model_texts,
                matching_model_texts,
            )
            for label in label_defs:
                count = len(weak_labels.get(label.name, set()))
                if count:
                    LOGGER.info("Weak label %s matches: %s", label.name, count)

    use_label_examples = any(label.positive_examples or label.negative_examples for label in label_defs)
    with log_startup_step("initialize embedding model"):
        embedder = SentenceTransformer(args.embedding_model) if use_label_examples else None
    with log_startup_step("compute embeddings"):
        embeddings = compute_embeddings(
            model_texts,
            model_name=args.embedding_model,
            cache_path=cache_path,
            render_spec=render_spec,
            embedder=embedder,
        )

    with log_startup_step("train models"):
        message_id_to_index = {message.message_id: idx for idx, message in enumerate(messages)}
        models = train_models(
            embeddings,
            label_defs,
            annotations,
            message_id_to_index,
            weak_labels,
            weak_label_weight=args.weak_label_weight,
            seed=args.seed,
            embedder=embedder,
        )

    with log_startup_step("predict probabilities"):
        probabilities = predict_probabilities(models, embeddings)
    if args.eval:
        with log_startup_step("evaluate models"):
            results = evaluate_models(
                label_defs,
                annotations,
                message_id_to_index,
                probabilities,
                threshold=args.eval_threshold,
            )
            if not results:
                LOGGER.error("No labeled annotations available for evaluation.")
                return 1
            log_evaluation_results(results, threshold=args.eval_threshold)
        return 0
    with log_startup_step("select candidates"):
        rng = random.Random(args.seed)
        candidates = select_candidates(
            messages,
            label_defs,
            probabilities,
            annotations,
            weak_labels,
            batch_size=args.batch_size,
            rng=rng,
            strategy=args.selection_strategy,
            random_sample_fraction=args.random_sample_fraction,
        )
    if not candidates:
        LOGGER.info("No candidates to annotate.")
        return 0
    random_total = sum(1 for candidate in candidates if candidate.randomly_chosen)
    if random_total:
        LOGGER.info(
            "Selected %s candidates (%s random, %s active).",
            len(candidates),
            random_total,
            len(candidates) - random_total,
        )

    with log_startup_step("prepare annotation loop"):
        min_before = 0
        if render_spec.mode == "prefix_author" and render_spec.prefix_max_messages is not None:
            min_before = max(0, render_spec.prefix_max_messages - 1)
        context_before, context_after = ensure_context_bounds(
            args.context_before,
            args.context_after,
            min_before=min_before,
        )
        max_content_length = args.max_content_length or None
        quote_window = max(args.quote_window, 1)

        autocomplete_options = build_autocomplete_options(label_defs)
        new_count = 0
        total_candidates = len(candidates)
        retrain_interval = args.retrain_interval
    for idx, candidate in enumerate(candidates, start=1):
        message = messages[candidate.index]
        weak_matches = [label.name for label in label_defs if candidate.index in weak_labels.get(label.name, set())]
        weak_label_defs = [label for label in label_defs if label.name in weak_matches]
        model_suggestions = (
            suggest_label_scores(probabilities, label_defs, candidate.index, limit=8) if probabilities else []
        )
        suggestions = build_suggestions(
            probabilities,
            label_defs,
            candidate.index,
            weak_matches,
            limit=8,
        )
        display_context(
            candidate.index,
            messages,
            channel_index,
            index_to_pos,
            normalized_contents,
            context_before=context_before,
            context_after=context_after,
            quote_window=quote_window,
            max_content_length=max_content_length,
            referenced_labels=weak_matches if args.show_weak_labels else None,
            weak_label_defs=weak_label_defs or None,
        )
        auto_labels = None
        if auto_annotator is not None:
            try:
                auto_labels = _auto_annotate_labels(
                    auto_annotator,
                    target_idx=candidate.index,
                    messages=messages,
                    channel_index=channel_index,
                    index_to_pos=index_to_pos,
                    normalized_contents=normalized_contents,
                    context_before=context_before,
                    context_after=context_after,
                    quote_window=quote_window,
                    max_content_length=max_content_length,
                    suggestions=model_suggestions,
                    label_defs=label_defs,
                )
            except Exception as exc:  # noqa: BLE001 - interactive feedback on failure
                LOGGER.warning("Auto-annotator failed: %s", exc)

        show_weak_prompt = args.show_weak_labels or not suggestions
        try:
            selected = prompt_for_labels(
                label_defs,
                suggestions=suggestions,
                weak_matches=weak_matches if show_weak_prompt else None,
                autocomplete_options=autocomplete_options,
                auto_labels=auto_labels,
            )
        except QuitAnnotation:
            LOGGER.info("Stopping annotation loop.")
            break
        if selected is None:
            LOGGER.info("Skipped %s", message.message_id)
            continue
        annotated_at = dt.datetime.now(dt.timezone.utc).isoformat()
        record = AnnotationRecord(
            message_id=message.message_id,
            channel_id=message.channel_id,
            labels=selected,
            label_names=[label.name for label in label_defs],
            created_at=annotated_at,
            annotated_at=annotated_at,
            content_hash=hash_content(model_texts[candidate.index]),
            annotator=args.annotator,
            render_spec=render_spec_payload(render_spec),
            selection_strategy=candidate.selection_strategy,
            selection_reason=candidate.selection_reason,
            randomly_chosen=candidate.randomly_chosen,
        )
        append_annotation(args.annotations, record)
        annotations[message.message_id] = record
        new_count += 1
        LOGGER.info("Saved annotation for %s: %s", message.message_id, ", ".join(selected) or "(none)")
        if retrain_interval and new_count % retrain_interval == 0 and idx < total_candidates:
            LOGGER.info("Retraining models after %s new annotations.", new_count)
            models = train_models(
                embeddings,
                label_defs,
                annotations,
                message_id_to_index,
                weak_labels,
                weak_label_weight=args.weak_label_weight,
                seed=args.seed,
                embedder=embedder,
            )
            probabilities = predict_probabilities(models, embeddings)

    LOGGER.info("Added %s annotations to %s", new_count, args.annotations)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
