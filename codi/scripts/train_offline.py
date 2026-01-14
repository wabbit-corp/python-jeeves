#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from codi.api.model.disentanglement.feature import Feature


class FeatureGroup(Protocol):
    @staticmethod
    def get_group_features() -> list[type[Feature]]: ...


def _add_codi_to_path() -> Path:
    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    return repo_root


def _resolve_features(
    names: Sequence[str],
    chat_cls: FeatureGroup,
    discourse_cls: FeatureGroup,
    content_cls: FeatureGroup,
) -> list[type[Feature]]:
    normalized = [name.lower() for name in names]
    if not normalized or "all" in normalized:
        normalized = ["chat", "discourse", "content"]

    features: list[type[Feature]] = []
    unknown: list[str] = []

    for name in normalized:
        if name == "chat":
            features.extend(chat_cls.get_group_features())
        elif name == "discourse":
            features.extend(discourse_cls.get_group_features())
        elif name == "content":
            features.extend(content_cls.get_group_features())
        else:
            unknown.append(name)

    if unknown:
        raise ValueError(f"Unknown features: {', '.join(unknown)}")

    return features


def _resolve_model_dir(repo_root: Path, model_dir: str | None) -> Path:
    if model_dir:
        path = Path(model_dir).expanduser().resolve()
        os.environ["CODI_MODEL_DIR"] = str(path)
        return path
    return repo_root / "codi" / "api" / "training" / "tmp" / "models"


def main() -> int:
    parser = argparse.ArgumentParser(description="Train CODI offline (no server).")
    parser.add_argument(
        "--training-json",
        required=True,
        help="Path to training JSON (CODI format).",
    )
    parser.add_argument(
        "--platform",
        choices=["discord", "slack"],
        help="Platform to set if the JSON does not include one.",
    )
    parser.add_argument(
        "--features",
        nargs="*",
        default=["chat", "discourse", "content"],
        help="Feature groups to use: chat, discourse, content, or all.",
    )
    parser.add_argument(
        "--model-dir",
        help="Directory to write model.pickle (sets CODI_MODEL_DIR).",
    )

    args = parser.parse_args()

    repo_root = _add_codi_to_path()

    from codi.api.model.disentanglement.chat import Chat
    from codi.api.model.disentanglement.content import Content
    from codi.api.model.disentanglement.discourse import Discourse
    from codi.api.model.disentanglement.model import Model
    from codi.api.model.input.community import Community

    training_path = Path(args.training_json).expanduser().resolve()
    with training_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    if args.platform:
        data["platform"] = args.platform
    elif "platform" not in data:
        raise ValueError("Training JSON missing platform and --platform not provided.")

    features = _resolve_features(args.features, Chat, Discourse, Content)
    model_dir = _resolve_model_dir(repo_root, args.model_dir)

    print(f"Training on: {training_path}")
    print(f"Platform: {data['platform']}")
    print(f"Features: {', '.join(args.features)}")
    print(f"Model dir: {model_dir}")

    community = Community().deserialize(data)
    model = Model()
    model.train(community, features=features)

    print("Training complete.")
    print(f"Model saved to: {model_dir / 'model.pickle'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
