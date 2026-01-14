from collections.abc import Sequence

def precision_recall_fscore_support(
    y_true: Sequence[int],
    y_pred: Sequence[int],
    average: str | None = ...,
    *args: object,
    **kwargs: object,
) -> tuple[object, object, object, object]: ...
