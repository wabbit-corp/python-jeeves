#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class LeafGroup:
    path: tuple[str, ...]
    cues: tuple[str, ...]


@dataclass(frozen=True)
class CueOverlap:
    cue: str
    groups: tuple[str, ...]
    forms: tuple[str, ...]


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("label_cue_overlap")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def coerce_str(value: object | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    text = str(value).strip()
    return text or None


def coerce_str_list(value: object | None, *, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    items: list[str] = []
    for item in value:
        if item is None:
            continue
        text = str(item).strip()
        if text:
            items.append(text)
    return items


def normalize_cue(cue: str, *, case_sensitive: bool) -> str:
    collapsed = " ".join(cue.strip().split())
    if case_sensitive:
        return collapsed
    return collapsed.casefold()


def load_yaml_labels(path: Path, *, logger: logging.Logger) -> list[object]:
    if not path.exists():
        raise FileNotFoundError(f"Label config not found: {path}")
    raw_text = path.read_text(encoding="utf-8")
    try:
        loaded = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ValueError(f"Failed to parse {path}: {exc}") from exc
    if not isinstance(loaded, list):
        raise ValueError("Label config must be a YAML list at the top level.")
    if not loaded:
        logger.warning("Label config %s is empty.", path)
    return loaded


def extract_leaf_groups(
    node: object,
    *,
    parents: tuple[str, ...],
    logger: logging.Logger,
) -> list[LeafGroup]:
    if not isinstance(node, dict):
        if parents:
            logger.warning("Skipping non-dict node under %s.", "/".join(parents))
        return []
    name = coerce_str(node.get("group"))
    if not name:
        logger.warning("Skipping group with missing name under %s.", "/".join(parents) or "<root>")
        return []
    path = (*parents, name)
    subgroups = node.get("subgroups")
    if subgroups is None:
        cues = coerce_str_list(node.get("cues"), field="cues")
        return [LeafGroup(path=path, cues=tuple(cues))]
    if not isinstance(subgroups, list):
        logger.warning("Skipping group %s with non-list subgroups.", "/".join(path))
        return []
    if not subgroups:
        cues = coerce_str_list(node.get("cues"), field="cues")
        return [LeafGroup(path=path, cues=tuple(cues))]
    leaves: list[LeafGroup] = []
    for child in subgroups:
        leaves.extend(extract_leaf_groups(child, parents=path, logger=logger))
    return leaves


def collect_leaf_groups(items: Iterable[object], *, logger: logging.Logger) -> list[LeafGroup]:
    leaves: list[LeafGroup] = []
    for entry in items:
        leaves.extend(extract_leaf_groups(entry, parents=(), logger=logger))
    return leaves


def build_overlaps(
    groups: Iterable[LeafGroup],
    *,
    case_sensitive: bool,
    min_groups: int,
) -> list[CueOverlap]:
    if min_groups < 2:
        raise ValueError("min_groups must be at least 2.")
    cue_to_groups: dict[str, set[str]] = defaultdict(set)
    cue_to_forms: dict[str, set[str]] = defaultdict(set)
    for group in groups:
        group_id = "/".join(group.path)
        seen: set[str] = set()
        for cue in group.cues:
            normalized = normalize_cue(cue, case_sensitive=case_sensitive)
            if not normalized:
                continue
            if normalized in seen:
                cue_to_forms[normalized].add(cue)
                continue
            seen.add(normalized)
            cue_to_groups[normalized].add(group_id)
            cue_to_forms[normalized].add(cue)
    overlaps: list[CueOverlap] = []
    for cue, group_ids in cue_to_groups.items():
        if len(group_ids) < min_groups:
            continue
        overlaps.append(
            CueOverlap(
                cue=cue,
                groups=tuple(sorted(group_ids)),
                forms=tuple(sorted(cue_to_forms[cue])),
            )
        )
    overlaps.sort(key=lambda item: (-len(item.groups), item.cue))
    return overlaps


def render_overlaps(
    overlaps: Sequence[CueOverlap],
    *,
    logger: logging.Logger,
    show_forms: bool,
    limit: int | None,
) -> None:
    if not overlaps:
        logger.info("No overlapping cues found.")
        return
    total = len(overlaps)
    if limit is None or limit <= 0:
        limit = total
    logger.info("Overlapping cues: %s", min(total, limit))
    for overlap in overlaps[:limit]:
        line = f"{overlap.cue} ({len(overlap.groups)} groups)"
        if show_forms:
            forms = [form for form in overlap.forms if form != overlap.cue]
            if forms:
                line = f"{line} | forms: {', '.join(forms)}"
        logger.info(line)
        for group in overlap.groups:
            logger.info("  - %s", group)


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List cue strings that appear in multiple label groups.",
    )
    parser.add_argument(
        "labels",
        nargs="?",
        default="labels.yml",
        help="Path to labels.yml (default: labels.yml).",
    )
    parser.add_argument(
        "--min-groups",
        type=int,
        default=2,
        help="Only show cues shared by at least this many groups (default: 2).",
    )
    parser.add_argument(
        "--case-sensitive",
        action="store_true",
        help="Match cues with case-sensitive comparisons.",
    )
    parser.add_argument(
        "--show-forms",
        action="store_true",
        help="Show original cue spellings when they differ from the normalized cue.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Limit the number of overlapping cues shown (0 means no limit).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logger = setup_logging()
    labels_path = Path(args.labels)
    try:
        items = load_yaml_labels(labels_path, logger=logger)
        groups = collect_leaf_groups(items, logger=logger)
        overlaps = build_overlaps(
            groups,
            case_sensitive=args.case_sensitive,
            min_groups=args.min_groups,
        )
    except (ValueError, FileNotFoundError) as exc:
        logger.error("%s", exc)
        return 2
    render_overlaps(
        overlaps,
        logger=logger,
        show_forms=args.show_forms,
        limit=args.limit if args.limit > 0 else None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
