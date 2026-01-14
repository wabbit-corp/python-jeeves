from __future__ import annotations

import logging

import aiohttp

from servant.api.rate_limit import RateLimiter
from typed_json import JSON, JSONDict, obj_to_json

_DROP_KEYS = {
    "family_friendly",
    "favicon",
    "img",
    "is_live",
    "is_source_both",
    "is_source_local",
    "language",
    "meta_url",
    "subtype",
    "thumbnail",
}


def _strip_keys(value: JSON) -> JSON:
    if isinstance(value, dict):
        return {k: _strip_keys(v) for k, v in value.items() if k not in _DROP_KEYS}
    if isinstance(value, list):
        return [_strip_keys(v) for v in value]
    return value


logger = logging.getLogger(__name__)


class BraveSearchApi:
    api_key: str | None

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        qps: float | None = None,
        rpm: int | None = None,
    ):
        self.api_key = api_key
        self.base_url = base_url or "https://api.search.brave.com"
        self._limiter = RateLimiter.from_limits(qps=qps, rpm=rpm)
        self._session: aiohttp.ClientSession | None = None

    def set_api_key(self, api_key: str) -> None:
        self.api_key = api_key

    async def __aenter__(self) -> BraveSearchApi:
        """Async context manager entry."""
        self._session = aiohttp.ClientSession(base_url=self.base_url)
        logger.info("BraveSearchApi session started.")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object | None,
    ) -> None:
        """Async context manager exit."""
        if self._session:
            await self._session.close()
            self._session = None
        logger.info("BraveSearchApi session closed.")

    async def search(self, query: str, num_results: int = 10) -> JSONDict:
        if not self.api_key:
            raise ValueError("API key is required for Brave Search API")
        if self._session is None:
            raise RuntimeError("BraveSearchApi session not initialized.")

        if self._limiter:
            await self._limiter.wait()

        headers = {"X-Subscription-Token": self.api_key, "Accept": "application/json"}
        params: dict[str, str | int] = {"q": query}
        count: int | None
        try:
            count = int(num_results)
        except (TypeError, ValueError):
            count = None
        if count and count > 0:
            params["count"] = count
        response = await self._session.get("/res/v1/web/search", headers=headers, params=params)
        response.raise_for_status()  # Raise an exception for bad status codes
        raw = await response.json()
        data_json = obj_to_json(raw)
        if not isinstance(data_json, dict):
            raise RuntimeError("Unexpected Brave Search response.")
        data: JSONDict = data_json
        data.pop("query", None)
        data.pop("mixed", None)
        if "news" in data and isinstance(data["news"], dict):
            data["news"] = data["news"].get("results", [])
        stripped = _strip_keys(data)
        if not isinstance(stripped, dict):
            raise RuntimeError("Unexpected Brave Search response.")
        # Charge after success
        return stripped
