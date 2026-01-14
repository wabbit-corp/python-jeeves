#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from openai import AsyncOpenAI

    from typed_json import JSONDict

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

MIN_CHUNK_OVERLAP_RATIO = 0.25
ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh"]


def _resolve_api_key(cli_key: str | None) -> str:
    if cli_key:
        return cli_key
    env_key = os.environ.get("OPENAI_API_KEY")
    if env_key:
        return env_key
    raise ValueError("OpenAI API key required (use --openai-key or OPENAI_API_KEY).")


def _resolve_overlap(chunk_size: int, overlap: int | None, overlap_ratio: float | None) -> int:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive.")
    if overlap is None:
        ratio = overlap_ratio if overlap_ratio is not None else 0.33
        overlap = max(1, int(chunk_size * ratio))
    if overlap >= chunk_size:
        raise ValueError("overlap must be smaller than chunk_size.")
    if overlap < int(chunk_size * MIN_CHUNK_OVERLAP_RATIO):
        raise ValueError(f"overlap must be at least {MIN_CHUNK_OVERLAP_RATIO:.0%} of chunk_size for reliable merging.")
    return overlap


def _normalize_reasoning_effort(value: str) -> ReasoningEffort:
    if value == "none":
        return "none"
    if value == "minimal":
        return "minimal"
    if value == "low":
        return "low"
    if value == "medium":
        return "medium"
    if value == "high":
        return "high"
    if value == "xhigh":
        return "xhigh"
    raise ValueError(f"Invalid reasoning_effort: {value}")


def _load_json(path: Path) -> JSONDict:
    from typed_json import obj_to_json, require_obj

    raw = json.loads(path.read_text(encoding="utf-8"))
    return require_obj(obj_to_json(raw))


def _write_json(path: Path, payload: JSONDict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def _collect_inputs(inputs: Sequence[str] | None, input_dir: str | None) -> list[Path]:
    paths: list[Path] = []
    if inputs:
        for value in inputs:
            paths.append(Path(value).expanduser().resolve())
    if input_dir:
        dir_path = Path(input_dir).expanduser().resolve()
        if not dir_path.is_dir():
            raise ValueError(f"input_dir is not a directory: {dir_path}")
        paths.extend(sorted(dir_path.glob("*.json")))
    if not paths:
        raise ValueError("No input files provided.")
    return paths


def _default_output_path(input_path: Path) -> Path:
    return input_path.parent / f"{input_path.stem}.annotated.json"


def _build_client(api_key: str) -> AsyncOpenAI:
    import openai

    return openai.AsyncOpenAI(api_key=api_key)


async def _annotate_file(
    client: AsyncOpenAI,
    input_path: Path,
    output_path: Path,
    *,
    chunk_size: int,
    overlap: int,
    max_message_chars: int,
    min_overlap_ratio: float,
    min_overlap_messages: int,
    model: str,
    max_completion_tokens: int,
    reasoning_effort: ReasoningEffort,
    retries: int,
    channel_ids: Sequence[str] | None,
) -> None:
    from servant.modules import codi_auto_annotator

    community = _load_json(input_path)
    config = codi_auto_annotator.AnnotatorConfig(
        chunk_size=chunk_size,
        overlap=overlap,
        min_overlap_ratio=min_overlap_ratio,
        min_overlap_messages=min_overlap_messages,
        max_message_chars=max_message_chars,
        model=model,
        max_completion_tokens=max_completion_tokens,
        reasoning_effort=reasoning_effort,
        retries=retries,
    )
    annotated = await codi_auto_annotator.annotate_community(
        client,
        community,
        config,
        channel_ids=channel_ids,
    )
    _write_json(output_path, annotated)


async def _run(args: argparse.Namespace) -> int:
    api_key = _resolve_api_key(args.openai_key)
    overlap = _resolve_overlap(args.chunk_size, args.overlap, args.overlap_ratio)
    reasoning_effort = _normalize_reasoning_effort(args.reasoning_effort)

    inputs = _collect_inputs(args.inputs, args.input_dir)
    if args.output and len(inputs) != 1:
        raise ValueError("--output can only be used with a single --input file.")

    client = _build_client(api_key)
    for path in inputs:
        output_path = (
            Path(args.output).expanduser().resolve()
            if args.output
            else (Path(args.out_dir).expanduser().resolve() / path.name if args.out_dir else _default_output_path(path))
        )
        await _annotate_file(
            client,
            path,
            output_path,
            chunk_size=args.chunk_size,
            overlap=overlap,
            max_message_chars=args.max_message_chars,
            min_overlap_ratio=args.min_overlap_ratio,
            min_overlap_messages=args.min_overlap_messages,
            model=args.model,
            max_completion_tokens=args.max_completion_tokens,
            reasoning_effort=reasoning_effort,
            retries=args.retries,
            channel_ids=args.channel_ids,
        )
        print(f"Wrote {output_path}")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Auto-annotate CODI community JSON using GPT-5.2.")
    parser.add_argument("--input", action="append", dest="inputs", help="Path to CODI community JSON (repeatable).")
    parser.add_argument("--input-dir", help="Directory containing CODI community JSON files.")
    parser.add_argument("--output", help="Output path for a single input file.")
    parser.add_argument("--out-dir", help="Output directory for multiple input files.")
    parser.add_argument("--channel-id", action="append", dest="channel_ids", help="Restrict to channel id(s).")
    parser.add_argument("--openai-key", help="OpenAI API key (or set OPENAI_API_KEY).")
    parser.add_argument("--model", default="gpt-5.2", help="OpenAI model to use.")
    parser.add_argument("--chunk-size", type=int, default=300, help="Messages per chunk (high N recommended).")
    parser.add_argument(
        "--overlap",
        type=int,
        help="Messages of overlap between chunks (must be >= 25% of chunk size).",
    )
    parser.add_argument(
        "--overlap-ratio",
        type=float,
        help="Overlap ratio if --overlap is not set (default 0.33).",
    )
    parser.add_argument(
        "--min-overlap-ratio",
        type=float,
        default=0.5,
        help="Minimum agreement ratio for mapping chunk labels to global labels.",
    )
    parser.add_argument(
        "--min-overlap-messages",
        type=int,
        default=3,
        help="Minimum number of overlapping messages required to map labels.",
    )
    parser.add_argument(
        "--max-message-chars",
        type=int,
        default=300,
        help="Max characters per message sent to the model.",
    )
    parser.add_argument(
        "--max-completion-tokens",
        type=int,
        default=6000,
        help="Max completion tokens for the model response.",
    )
    parser.add_argument(
        "--reasoning-effort",
        default="low",
        help="Reasoning effort for the model (if supported).",
    )
    parser.add_argument("--retries", type=int, default=1, help="Retries per chunk on parsing failures.")

    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
