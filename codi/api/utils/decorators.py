import time
import traceback
from collections.abc import Callable
from typing import ParamSpec, TypeVar

from rest_framework import status
from rest_framework.response import Response

P = ParamSpec("P")
R = TypeVar("R")


def error_handling(func: Callable[P, R]) -> Callable[P, Response | R]:
    def inner(*args: P.args, **kwargs: P.kwargs) -> Response | R:
        try:
            return func(*args, **kwargs)
        except Exception as e:
            error = {"error": str(e)}
            traceback.print_exc()

            return Response(status=status.HTTP_400_BAD_REQUEST, data=error)

    return inner


def measure_time(func: Callable[P, R]) -> Callable[P, tuple[R, float]]:
    def inner(*args: P.args, **kwargs: P.kwargs) -> tuple[R, float]:
        start = time.time()
        res = func(*args, **kwargs)
        stop = time.time()

        return res, round(stop - start, 3)

    return inner
