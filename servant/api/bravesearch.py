from __future__ import annotations

import aiohttp
import logging

from servant.api.rate_limit import RateLimiter

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


def _strip_keys(value):
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

    def set_api_key(self, api_key: str):
        self.api_key = api_key

    async def __aenter__(self):
        """Async context manager entry."""
        self._session = aiohttp.ClientSession(base_url=self.base_url)
        logger.info("BraveSearchApi session started.")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        if self._session:
            await self._session.close()
        logger.info("BraveSearchApi session closed.")

    async def search(self, query: str, num_results: int = 10) -> dict:
        if not self.api_key:
            raise ValueError("API key is required for Brave Search API")

        if self._limiter:
            await self._limiter.wait()

        headers = {"X-Subscription-Token": self.api_key, "Accept": "application/json"}
        params = {"q": query}
        try:
            count = int(num_results)
        except (TypeError, ValueError):
            count = None
        if count and count > 0:
            params["count"] = count
        response = await self._session.get(
            "/res/v1/web/search", headers=headers, params=params
        )
        response.raise_for_status()  # Raise an exception for bad status codes
        data = await response.json()
        data.pop("query", None)
        data.pop("mixed", None)
        if "news" in data and isinstance(data["news"], dict):
            data["news"] = data["news"].get("results", [])
        data = _strip_keys(data)
        # Charge after success
        return data
