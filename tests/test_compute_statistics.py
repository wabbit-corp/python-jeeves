import pytest
import tqdm
from hypothesis import given
from hypothesis import strategies as st

from codi.api.utils import compute_statistics


@st.composite
def _label_pairs(draw: st.DrawFn) -> tuple[list[int], list[int]]:
    length = draw(st.integers(min_value=1, max_value=8))
    gold = draw(st.lists(st.integers(min_value=0, max_value=2), min_size=length, max_size=length))
    pred = draw(st.lists(st.integers(min_value=0, max_value=2), min_size=length, max_size=length))
    return gold, pred


@st.composite
def _labels_with_positives(draw: st.DrawFn) -> tuple[list[int], list[int]]:
    length = draw(st.integers(min_value=1, max_value=12))
    labels = draw(st.lists(st.integers(min_value=0, max_value=1), min_size=length, max_size=length))
    if 1 not in labels:
        idx = draw(st.integers(min_value=0, max_value=length - 1))
        labels[idx] = 1
    preds = draw(st.lists(st.integers(min_value=0, max_value=1), min_size=length, max_size=length))
    return labels, preds


@st.composite
def _labels_with_no_predicted_positives(draw: st.DrawFn) -> tuple[list[int], list[int]]:
    length = draw(st.integers(min_value=1, max_value=12))
    labels = draw(st.lists(st.integers(min_value=0, max_value=1), min_size=length, max_size=length))
    if 1 not in labels:
        idx = draw(st.integers(min_value=0, max_value=length - 1))
        labels[idx] = 1
    preds = [0] * length
    return labels, preds


@given(_label_pairs())
def test_micro_averaged_f_score_in_bounds(pair: tuple[list[int], list[int]]) -> None:
    gold, pred = pair
    stats: compute_statistics.Statistics = {}
    patch = pytest.MonkeyPatch()
    try:
        patch.setattr(tqdm, "tqdm", lambda x: x)
        score = compute_statistics.micro_averaged_f_score_labels(stats, gold, pred)

        assert 0.0 <= score <= 1.0
        assert "Combined-clustering" in stats
        assert stats["Combined-clustering"]["f1_score"] == score
    finally:
        patch.undo()


@given(_label_pairs())
def test_micro_averaged_f_score_with_feature_group(pair: tuple[list[int], list[int]]) -> None:
    gold, pred = pair
    stats: compute_statistics.Statistics = {}
    patch = pytest.MonkeyPatch()
    try:
        patch.setattr(tqdm, "tqdm", lambda x: x)
        score = compute_statistics.micro_averaged_f_score_labels(stats, gold, pred, feature_group="topics")

        assert 0.0 <= score <= 1.0
        assert "topics-clustering" in stats
    finally:
        patch.undo()


@given(_labels_with_positives())
def test_f_score_records_times(pair: tuple[list[int], list[int]]) -> None:
    labels, preds = pair
    stats: compute_statistics.Statistics = {}

    compute_statistics.f_score(stats, labels, preds, times={"total": 1.0})

    assert "Combined" in stats
    entry = stats["Combined"]
    accuracy = entry.get("accuracy")
    assert isinstance(accuracy, float)
    assert 0.0 <= accuracy <= 1.0
    assert entry["times"] == {"total": 1.0}


@given(_labels_with_no_predicted_positives())
def test_f_score_handles_no_predicted_positives(pair: tuple[list[int], list[int]]) -> None:
    labels, preds = pair
    stats: compute_statistics.Statistics = {}

    compute_statistics.f_score(stats, labels, preds, feature_group="zeros")

    assert stats["zeros"]["f1_score"] == 0.0
