from collections.abc import Sequence

class LogisticRegression:
    def __init__(
        self,
        random_state: int = ...,
        tol: float = ...,
        max_iter: int = ...,
        penalty: str = ...,
    ) -> None: ...
    def fit(
        self,
        X: object,
        y: object,
        sample_weight: object | None = ...,
    ) -> LogisticRegression: ...
    def predict(self, X: object) -> Sequence[int]: ...
    def predict_proba(self, X: object) -> Sequence[Sequence[float]]: ...
