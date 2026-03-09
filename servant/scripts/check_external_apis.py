#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import logging
import socket
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlparse

import aiohttp

_LOGGER = logging.getLogger(__name__)

_DEFAULT_HEADERS = {"User-Agent": "jeeves-api-check/1.0"}


@dataclass(frozen=True)
class Endpoint:
    name: str
    url: str
    method: str = "GET"
    headers: dict[str, str] | None = None
    params: dict[str, str] | None = None
    data: dict[str, str] | None = None
    ok_statuses: tuple[int, ...] = (200,)
    expect_json: bool = True
    note: str | None = None


@dataclass(frozen=True)
class ResolveResult:
    ipv4: tuple[str, ...]
    ipv6: tuple[str, ...]


@dataclass(frozen=True)
class AttemptResult:
    mode: str
    status: int | None
    elapsed_s: float | None
    ok: bool
    warning: str | None
    error: str | None
    skipped: bool


@dataclass
class Summary:
    ok: int = 0
    warn: int = 0
    fail: int = 0
    skipped: int = 0


def _build_endpoints() -> tuple[Endpoint, ...]:
    sol_mint = "So11111111111111111111111111111111111111112"
    usdc_mint = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    quote_params = {
        "inputMint": sol_mint,
        "outputMint": usdc_mint,
        "amount": "10000000",
        "slippageBps": "50",
        "onlyDirectRoutes": "false",
    }
    return (
        Endpoint(
            name="Brave Search API",
            url="https://api.search.brave.com/res/v1/web/search",
            params={"q": "ping"},
            ok_statuses=(200, 401, 403),
            expect_json=True,
            note="401/403 expected without API key",
        ),
        Endpoint(
            name="Imgflip get_memes",
            url="https://api.imgflip.com/get_memes",
            ok_statuses=(200,),
            expect_json=True,
        ),
        Endpoint(
            name="Imgflip caption_image",
            url="https://api.imgflip.com/caption_image",
            method="POST",
            data={
                "template_id": "0",
                "username": "missing",
                "password": "missing",
            },
            ok_statuses=(200,),
            expect_json=True,
            note="Expected to fail auth/params but should return JSON",
        ),
        Endpoint(
            name="Open-Meteo forecast",
            url="https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": "0",
                "longitude": "0",
                "current": "temperature_2m",
            },
            ok_statuses=(200,),
            expect_json=True,
        ),
        Endpoint(
            name="Wikipedia summary",
            url="https://en.wikipedia.org/api/rest_v1/page/summary/Python_(programming_language)",
            ok_statuses=(200,),
            expect_json=True,
        ),
        Endpoint(
            name="Discord gateway",
            url="https://discord.com/api/v10/gateway",
            ok_statuses=(200,),
            expect_json=True,
        ),
        Endpoint(
            name="GitHub API",
            url="https://api.github.com/",
            ok_statuses=(200, 301),
            expect_json=True,
        ),
        Endpoint(
            name="OpenAI models",
            url="https://api.openai.com/v1/models",
            ok_statuses=(200, 401, 403),
            expect_json=True,
            note="401/403 expected without API key",
        ),
        Endpoint(
            name="Jupiter Lite quote",
            url="https://lite-api.jup.ag/swap/v1/quote",
            params=quote_params,
            ok_statuses=(200,),
            expect_json=True,
        ),
        Endpoint(
            name="Jupiter Lite quote (OPTIONS)",
            url="https://lite-api.jup.ag/swap/v1/quote",
            method="OPTIONS",
            ok_statuses=(200, 204, 405),
            expect_json=False,
            note="OPTIONS preflight; 405 means not allowed but reachable",
        ),
        Endpoint(
            name="Jupiter Lite tokens v2 search",
            url="https://lite-api.jup.ag/tokens/v2/search",
            params={"query": "SOL"},
            ok_statuses=(200,),
            expect_json=True,
        ),
        Endpoint(
            name="Jupiter Lite tokens v2 search (OPTIONS)",
            url="https://lite-api.jup.ag/tokens/v2/search",
            method="OPTIONS",
            ok_statuses=(200, 204, 405),
            expect_json=False,
            note="OPTIONS preflight; 405 means not allowed but reachable",
        ),
        Endpoint(
            name="Jupiter API quote",
            url="https://api.jup.ag/swap/v1/quote",
            params=quote_params,
            ok_statuses=(200, 401, 403),
            expect_json=True,
            note="401/403 expected without API key",
        ),
        Endpoint(
            name="Jupiter API quote (OPTIONS)",
            url="https://api.jup.ag/swap/v1/quote",
            method="OPTIONS",
            ok_statuses=(200, 204, 401, 403, 405),
            expect_json=False,
            note="OPTIONS preflight; 405 means not allowed but reachable",
        ),
        Endpoint(
            name="Jupiter Prediction Market events",
            url="https://prediction-market-api.jup.ag/api/v1/events",
            ok_statuses=(200, 401, 403),
            expect_json=True,
        ),
        Endpoint(
            name="Jupiter Prediction Market events (OPTIONS)",
            url="https://prediction-market-api.jup.ag/api/v1/events",
            method="OPTIONS",
            ok_statuses=(200, 204, 401, 403, 405),
            expect_json=False,
            note="OPTIONS preflight; 405 means not allowed but reachable",
        ),
        Endpoint(
            name="Jupiter Perps market-stats",
            url="https://perps-api.jup.ag/v1/market-stats",
            params={"mint": sol_mint},
            ok_statuses=(200, 400),
            expect_json=True,
            note="400 can indicate missing market data but confirms reachability",
        ),
        Endpoint(
            name="Jupiter Perps market-stats (OPTIONS)",
            url="https://perps-api.jup.ag/v1/market-stats",
            method="OPTIONS",
            ok_statuses=(200, 204, 400, 405),
            expect_json=False,
            note="OPTIONS preflight; 405 means not allowed but reachable",
        ),
        Endpoint(
            name="YouTube timedtext",
            url="https://www.youtube.com/api/timedtext",
            params={"lang": "en", "v": "dQw4w9WgXcQ"},
            ok_statuses=(200, 204),
            expect_json=False,
            note="200/204 expected; 404 implies transcript missing",
        ),
    )


def _parse_host_port(url: str) -> tuple[str, int]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"Unsupported URL scheme in {url!r}")
    host = parsed.hostname
    if not host:
        raise ValueError(f"Missing host in {url!r}")
    if parsed.port is not None:
        port = parsed.port
    else:
        port = 443 if parsed.scheme == "https" else 80
    return host, port


def _is_ipv4_mapped(addr: str) -> bool:
    return addr.lower().startswith("::ffff:")


async def _resolve_family(host: str, port: int, family: int) -> tuple[str, ...]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, family=family, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return ()
    addrs: list[str] = []
    for info in infos:
        addr = info[4][0]
        if family == socket.AF_INET6 and _is_ipv4_mapped(addr):
            continue
        if addr not in addrs:
            addrs.append(addr)
    return tuple(addrs)


async def _resolve_host(host: str, port: int) -> ResolveResult:
    ipv4 = await _resolve_family(host, port, socket.AF_INET)
    ipv6 = await _resolve_family(host, port, socket.AF_INET6)
    return ResolveResult(ipv4=ipv4, ipv6=ipv6)


def _format_addrs(addrs: Iterable[str]) -> str:
    items = list(addrs)
    if not items:
        return "-"
    return ",".join(items)


async def _attempt_request(
    session: aiohttp.ClientSession,
    endpoint: Endpoint,
) -> tuple[int | None, str | None, str | None, float | None]:
    start = time.monotonic()
    try:
        headers = dict(_DEFAULT_HEADERS)
        if endpoint.headers:
            headers.update(endpoint.headers)
        async with session.request(
            endpoint.method.upper(),
            endpoint.url,
            headers=headers,
            params=endpoint.params,
            data=endpoint.data,
            allow_redirects=True,
        ) as response:
            status = response.status
            content_type = response.headers.get("Content-Type", "")
            body = await response.content.read(2048)
            elapsed = time.monotonic() - start
            warning: str | None = None
            if endpoint.expect_json and "application/json" not in content_type.lower():
                body_hint = body.decode("utf-8", errors="ignore").strip().lower()
                snippet = body_hint[:120]
                warning = f"unexpected content-type {content_type!r}; body starts with {snippet!r}"
            return status, warning, None, elapsed
    except asyncio.TimeoutError:
        elapsed = time.monotonic() - start
        return None, None, "timeout", elapsed
    except aiohttp.ClientError as exc:
        elapsed = time.monotonic() - start
        return None, None, str(exc), elapsed


async def _check_endpoint(
    endpoint: Endpoint,
    sessions: dict[str, aiohttp.ClientSession],
    resolver_cache: dict[tuple[str, int], ResolveResult],
    summary: Summary,
) -> None:
    host, port = _parse_host_port(endpoint.url)
    cache_key = (host, port)
    if cache_key in resolver_cache:
        resolved = resolver_cache[cache_key]
    else:
        resolved = await _resolve_host(host, port)
        resolver_cache[cache_key] = resolved

    _LOGGER.info("Endpoint: %s", endpoint.name)
    _LOGGER.info("  URL: %s", endpoint.url)
    if endpoint.note:
        _LOGGER.info("  Note: %s", endpoint.note)
    _LOGGER.info("  DNS: A=%s AAAA=%s", _format_addrs(resolved.ipv4), _format_addrs(resolved.ipv6))

    for mode, session in sessions.items():
        if mode == "ipv4" and not resolved.ipv4:
            summary.skipped += 1
            _LOGGER.info("  %s: skipped (no IPv4 records)", mode)
            continue
        if mode == "ipv6" and not resolved.ipv6:
            summary.skipped += 1
            _LOGGER.info("  %s: skipped (no IPv6 records)", mode)
            continue

        status, warning, error, elapsed = await _attempt_request(session, endpoint)
        ok = status in endpoint.ok_statuses if status is not None else False

        if error is not None:
            summary.fail += 1
            _LOGGER.error("  %s: error after %.2fs: %s", mode, elapsed or 0.0, error)
            continue

        if status is None:
            summary.fail += 1
            _LOGGER.error("  %s: error (no status)", mode)
            continue

        if ok and warning is None:
            summary.ok += 1
            _LOGGER.info("  %s: HTTP %s in %.2fs", mode, status, elapsed or 0.0)
        else:
            summary.warn += 1
            if warning:
                _LOGGER.warning("  %s: HTTP %s in %.2fs (%s)", mode, status, elapsed or 0.0, warning)
            else:
                _LOGGER.warning("  %s: HTTP %s in %.2fs (unexpected status)", mode, status, elapsed or 0.0)


async def _run_checks(args: argparse.Namespace) -> int:
    endpoints = _build_endpoints()
    if not endpoints:
        _LOGGER.error("No endpoints configured.")
        return 1

    modes: list[str] = []
    if not args.no_default:
        modes.append("default")
    if not args.no_ipv4:
        modes.append("ipv4")
    if not args.no_ipv6:
        modes.append("ipv6")

    if not modes:
        _LOGGER.error("No modes selected. Enable at least one of default/ipv4/ipv6.")
        return 1

    timeout = aiohttp.ClientTimeout(total=args.timeout)
    sessions: dict[str, aiohttp.ClientSession] = {}

    if "default" in modes:
        sessions["default"] = aiohttp.ClientSession(timeout=timeout)
    if "ipv4" in modes:
        sessions["ipv4"] = aiohttp.ClientSession(
            timeout=timeout,
            connector=aiohttp.TCPConnector(family=socket.AF_INET),
        )
    if "ipv6" in modes:
        sessions["ipv6"] = aiohttp.ClientSession(
            timeout=timeout,
            connector=aiohttp.TCPConnector(family=socket.AF_INET6),
        )

    resolver_cache: dict[tuple[str, int], ResolveResult] = {}
    summary = Summary()

    try:
        for endpoint in endpoints:
            await _check_endpoint(endpoint, sessions, resolver_cache, summary)
    finally:
        await asyncio.gather(*(session.close() for session in sessions.values()))

    _LOGGER.info(
        "Summary: ok=%s warn=%s fail=%s skipped=%s",
        summary.ok,
        summary.warn,
        summary.fail,
        summary.skipped,
    )

    return 1 if summary.fail else 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check external API connectivity using aiohttp.")
    parser.add_argument(
        "--timeout",
        type=float,
        default=8.0,
        help="Request timeout in seconds (default: 8).",
    )
    parser.add_argument(
        "--no-default",
        action="store_true",
        help="Skip default resolver (happy eyeballs).",
    )
    parser.add_argument(
        "--no-ipv4",
        action="store_true",
        help="Skip IPv4-only checks.",
    )
    parser.add_argument(
        "--no-ipv6",
        action="store_true",
        help="Skip IPv6-only checks.",
    )
    return parser


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = _build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(_run_checks(args))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
