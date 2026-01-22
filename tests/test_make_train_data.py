from __future__ import annotations

import json
import random
from pathlib import Path

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from make_train_data import (
    AnnotationRecord,
    _build_label_definitions,
    _compile_cue_pattern,
    _parse_auto_annotator_labels,
    build_training_data,
    build_channel_index,
    build_prefix_texts,
    load_annotations,
    Message,
    normalize_for_matching,
    RenderSpec,
    select_candidates,
)


@given(st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), min_size=1))
def test_compile_cue_pattern_matches_literal(text: str) -> None:
    assume(text.strip() != "")
    assume(not text.lower().startswith("re:"))
    pattern = _compile_cue_pattern(text)
    assert pattern.search(text) is not None


def test_compile_cue_pattern_literal_punctuation() -> None:
    pattern = _compile_cue_pattern("$25/hr")
    assert pattern.search("Pay is $25/hr today.") is not None

    pattern = _compile_cue_pattern("package handler (remote)")
    assert pattern.search("Hiring: package handler (remote) now.") is not None
    assert pattern.search("package handler remote") is None


def test_compile_cue_pattern_explicit_regex() -> None:
    pattern = _compile_cue_pattern(r"re:\btester\b")
    assert pattern.search("tester") is not None
    assert pattern.search("testing") is None


def test_compile_cue_pattern_avoids_substring_match() -> None:
    pattern = _compile_cue_pattern("cat")
    assert pattern.search("concatenate") is None
    assert pattern.search("a cat appears") is not None


def test_compile_cue_pattern_invalid_regex() -> None:
    with pytest.raises(ValueError):
        _compile_cue_pattern("re:[")


def test_build_training_data_weights() -> None:
    annotations = {
        "m1": AnnotationRecord(
            message_id="m1",
            channel_id="c1",
            labels=["spam"],
            label_names=[],
            created_at="1",
            annotated_at=None,
            content_hash="hash1",
            annotator=None,
            render_spec=None,
            selection_strategy=None,
            selection_reason=None,
            randomly_chosen=None,
        ),
        "m2": AnnotationRecord(
            message_id="m2",
            channel_id="c1",
            labels=[],
            label_names=["spam"],
            created_at="2",
            annotated_at=None,
            content_hash="hash2",
            annotator=None,
            render_spec=None,
            selection_strategy=None,
            selection_reason=None,
            randomly_chosen=None,
        ),
    }
    message_id_to_index = {"m1": 0, "m2": 1, "m3": 2}
    weak_labels = {"spam": {2}}

    indices, labels, weights = build_training_data(
        "spam",
        annotations,
        message_id_to_index,
        weak_labels,
        0.2,
    )

    index_to_label = dict(zip(indices.tolist(), labels.tolist()))
    index_to_weight = dict(zip(indices.tolist(), weights.tolist()))

    assert set(index_to_label) == {0, 1, 2}
    assert index_to_label[0] == 1
    assert index_to_label[1] == 0
    assert index_to_label[2] == 1
    assert index_to_weight[0] == pytest.approx(1.0)
    assert index_to_weight[1] == pytest.approx(1.0)
    assert index_to_weight[2] == pytest.approx(0.2)


def test_build_label_definitions_examples() -> None:
    definitions = _build_label_definitions(
        [
            {
                "group": "demo_label",
                "description": "Example label.",
                "cues": [],
                "examples": {
                    "positives": ["Good example", "  "],
                    "negatives": ["Bad example", None],
                },
            }
        ]
    )

    assert definitions[0].positive_examples == ("Good example",)
    assert definitions[0].negative_examples == ("Bad example",)


def test_normalize_for_matching_handles_punctuation() -> None:
    assert normalize_for_matching("Open-to-work!") == "open to work"


def test_build_prefix_texts_consecutive_author() -> None:
    messages = [
        Message(
            index=0,
            message_id="a",
            channel_id="c1",
            author_id="u1",
            content="A",
            created_at="1",
            deleted_at=None,
            mention_ids=[],
            attachment_count=0,
        ),
        Message(
            index=1,
            message_id="b",
            channel_id="c1",
            author_id="u1",
            content="B",
            created_at="2",
            deleted_at=None,
            mention_ids=[],
            attachment_count=0,
        ),
        Message(
            index=2,
            message_id="c",
            channel_id="c1",
            author_id="u2",
            content="C",
            created_at="3",
            deleted_at=None,
            mention_ids=[],
            attachment_count=0,
        ),
        Message(
            index=3,
            message_id="d",
            channel_id="c1",
            author_id="u1",
            content="D",
            created_at="4",
            deleted_at=None,
            mention_ids=[],
            attachment_count=0,
        ),
    ]
    channel_index, _ = build_channel_index(messages)
    rendered = build_prefix_texts(
        messages,
        channel_index,
        max_messages=3,
        max_gap_seconds=60,
        max_chars=100,
        boundary_re=None,
    )
    assert rendered[0] == "A"
    assert rendered[1] == "A\nB"
    assert rendered[2] == "C"
    assert rendered[3] == "D"


def test_build_prefix_texts_respects_gap() -> None:
    messages = [
        Message(
            index=0,
            message_id="a",
            channel_id="c1",
            author_id="u1",
            content="A",
            created_at="1",
            deleted_at=None,
            mention_ids=[],
            attachment_count=0,
        ),
        Message(
            index=1,
            message_id="b",
            channel_id="c1",
            author_id="u1",
            content="B",
            created_at="1000",
            deleted_at=None,
            mention_ids=[],
            attachment_count=0,
        ),
    ]
    channel_index, _ = build_channel_index(messages)
    rendered = build_prefix_texts(
        messages,
        channel_index,
        max_messages=5,
        max_gap_seconds=10,
        max_chars=100,
        boundary_re=None,
    )
    assert rendered[0] == "A"
    assert rendered[1] == "B"


def test_build_prefix_texts_missing_timestamps_boundary() -> None:
    messages = [
        Message(
            index=0,
            message_id="a",
            channel_id="c1",
            author_id="u1",
            content="A",
            created_at=None,
            deleted_at=None,
            mention_ids=[],
            attachment_count=0,
        ),
        Message(
            index=1,
            message_id="b",
            channel_id="c1",
            author_id="u1",
            content="B",
            created_at=None,
            deleted_at=None,
            mention_ids=[],
            attachment_count=0,
        ),
    ]
    channel_index, _ = build_channel_index(messages)
    rendered = build_prefix_texts(
        messages,
        channel_index,
        max_messages=5,
        max_gap_seconds=60,
        max_chars=100,
        boundary_re=None,
    )
    assert rendered[0] == "A"
    assert rendered[1] == "B"


def test_load_annotations_migrates_legacy(tmp_path: Path) -> None:
    messages = [
        Message(
            index=0,
            message_id="a",
            channel_id="c1",
            author_id="u1",
            content="A",
            created_at="1",
            deleted_at=None,
            mention_ids=[],
            attachment_count=0,
        ),
        Message(
            index=1,
            message_id="b",
            channel_id="c1",
            author_id="u1",
            content="B",
            created_at="2",
            deleted_at=None,
            mention_ids=[],
            attachment_count=0,
        ),
    ]
    channel_index, _ = build_channel_index(messages)
    model_texts = build_prefix_texts(
        messages,
        channel_index,
        max_messages=2,
        max_gap_seconds=60,
        max_chars=100,
        boundary_re=None,
    )
    annotation_path = tmp_path / "annotations.jsonl"
    payload = {
        "message_id": "b",
        "channel_id": "c1",
        "labels": ["spam"],
        "label_names": ["spam"],
        "created_at": "1",
        "content_hash": "legacy-hash",
        "annotator": None,
    }
    annotation_path.write_text(f"{json.dumps(payload)}\n", encoding="utf-8")
    records = load_annotations(
        annotation_path,
        messages,
        model_texts=model_texts,
        render_spec=RenderSpec(
            mode="prefix_author",
            prefix_max_messages=2,
            prefix_max_gap_seconds=60,
            prefix_max_chars=100,
            boundary_regex=None,
        ),
        allow_mismatched=False,
    )
    assert "b" in records
    assert records["b"].content_hash != "legacy-hash"


def test_select_candidates_adds_random_samples() -> None:
    messages = [
        Message(
            index=0,
            message_id="a",
            channel_id="c1",
            author_id="u1",
            content="A",
            created_at="1",
            deleted_at=None,
            mention_ids=[],
            attachment_count=0,
        ),
        Message(
            index=1,
            message_id="b",
            channel_id="c1",
            author_id="u1",
            content="B",
            created_at="2",
            deleted_at=None,
            mention_ids=[],
            attachment_count=0,
        ),
        Message(
            index=2,
            message_id="c",
            channel_id="c1",
            author_id="u1",
            content="C",
            created_at="3",
            deleted_at=None,
            mention_ids=[],
            attachment_count=0,
        ),
        Message(
            index=3,
            message_id="d",
            channel_id="c1",
            author_id="u1",
            content="D",
            created_at="4",
            deleted_at=None,
            mention_ids=[],
            attachment_count=0,
        ),
        Message(
            index=4,
            message_id="e",
            channel_id="c1",
            author_id="u1",
            content="E",
            created_at="5",
            deleted_at=None,
            mention_ids=[],
            attachment_count=0,
        ),
    ]
    candidates = select_candidates(
        messages,
        label_defs=[],
        probabilities={},
        annotations={},
        weak_labels={},
        batch_size=5,
        rng=random.Random(13),
        strategy="uncertainty",
        random_sample_fraction=0.4,
    )
    assert len(candidates) == 5
    assert sum(1 for candidate in candidates if candidate.randomly_chosen) == 2


def test_parse_auto_annotator_labels_valid() -> None:
    labels = _parse_auto_annotator_labels('{"labels": ["spam", "ham", "spam"]}', {"spam", "ham"})
    assert labels == ["ham", "spam"]


def test_parse_auto_annotator_labels_embedded_json() -> None:
    labels = _parse_auto_annotator_labels('result={"labels": ["spam"]} done', {"spam"})
    assert labels == ["spam"]


def test_parse_auto_annotator_labels_unknown() -> None:
    with pytest.raises(ValueError):
        _parse_auto_annotator_labels('{"labels": ["spam"]}', {"ham"})
