import asyncio

import httpx
import pytest

from pumpbot.config import Tier2FilterConfig
from pumpbot.curve import BondingCurveState
from pumpbot.filters.tier1 import Candidate
from pumpbot.filters.tier2 import (
    FetchOutcome,
    MetadataCache,
    MetadataResult,
    Tier2Filter,
    fetch_metadata,
    score_metadata,
)

CONFIG = Tier2FilterConfig(
    enabled=True,
    mode="enforce",
    min_socials=1,
    fetch_timeout_seconds=0.2,
    fail_closed=False,
    cache_max_entries=1000,
)

FRESH_CURVE = BondingCurveState(
    virtual_token_reserves=1_073_000_000_000_000,
    virtual_sol_reserves=30_000_000_000,
    real_token_reserves=793_100_000_000_000,
    real_sol_reserves=0,
    token_total_supply=1_000_000_000_000_000,
    complete=False,
)


def make_candidate(**overrides) -> Candidate:
    defaults = {
        "mint": "MintAddr111",
        "creator": "CreatorAddr111",
        "name": "Some Coin",
        "symbol": "SOME",
        "creator_supply_fraction": 0.01,
        "curve": FRESH_CURVE,
        "uri": "https://meta.example.com/token.json",
    }
    defaults.update(overrides)
    return Candidate(**defaults)


class _CountingSyncTransport(httpx.BaseTransport, httpx.AsyncBaseTransport):
    """Wraps a sync handler, also serving async clients, and counts calls
    (to prove cache hits skip the network)."""

    def __init__(self, handler) -> None:
        self._handler = handler
        self.calls = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return self._handler(request)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return self._handler(request)


class _NeverCalledTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise AssertionError("transport should never be called for this case")


class _SlowTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(10)
        return httpx.Response(200, json={})


def client_for(handler) -> tuple[httpx.AsyncClient, _CountingSyncTransport]:
    transport = _CountingSyncTransport(handler)
    return httpx.AsyncClient(transport=transport), transport


# --- fetch_metadata: five outcome categories ---------------------------


@pytest.mark.asyncio
async def test_fetch_metadata_has_socials():
    def handler(request):
        return httpx.Response(200, json={"twitter": "https://x.com/foo", "telegram": "", "website": ""})

    client, _ = client_for(handler)
    async with client:
        result = await fetch_metadata(client, "https://meta.example.com/a.json", timeout=0.2)
    assert result.outcome == FetchOutcome.HAS_SOCIALS
    assert result.has_twitter is True
    assert result.has_telegram is False


@pytest.mark.asyncio
async def test_fetch_metadata_no_socials():
    def handler(request):
        return httpx.Response(200, json={"twitter": "", "telegram": "", "website": ""})

    client, _ = client_for(handler)
    async with client:
        result = await fetch_metadata(client, "https://meta.example.com/a.json", timeout=0.2)
    assert result.outcome == FetchOutcome.NO_SOCIALS
    assert not (result.has_twitter or result.has_telegram or result.has_website)


@pytest.mark.asyncio
async def test_fetch_metadata_fetch_failed_on_network_error():
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    client, _ = client_for(handler)
    async with client:
        result = await fetch_metadata(client, "https://meta.example.com/a.json", timeout=0.2)
    assert result.outcome == FetchOutcome.FETCH_FAILED


@pytest.mark.asyncio
async def test_fetch_metadata_fetch_failed_on_invalid_json():
    def handler(request):
        return httpx.Response(200, content=b"not json at all")

    client, _ = client_for(handler)
    async with client:
        result = await fetch_metadata(client, "https://meta.example.com/a.json", timeout=0.2)
    assert result.outcome == FetchOutcome.FETCH_FAILED


@pytest.mark.asyncio
async def test_fetch_metadata_fetch_timeout():
    client = httpx.AsyncClient(transport=_SlowTransport())
    async with client:
        result = await fetch_metadata(client, "https://meta.example.com/a.json", timeout=0.05)
    assert result.outcome == FetchOutcome.FETCH_TIMEOUT


# --- SSRF / hostile-input guards -----------------------------------------


@pytest.mark.asyncio
async def test_fetch_metadata_rejects_non_https_scheme():
    client = httpx.AsyncClient(transport=_NeverCalledTransport())
    async with client:
        result = await fetch_metadata(client, "http://meta.example.com/a.json", timeout=0.2)
    assert result.outcome == FetchOutcome.URI_REJECTED


@pytest.mark.asyncio
async def test_fetch_metadata_rejects_file_scheme():
    client = httpx.AsyncClient(transport=_NeverCalledTransport())
    async with client:
        result = await fetch_metadata(client, "file:///etc/passwd", timeout=0.2)
    assert result.outcome == FetchOutcome.URI_REJECTED


@pytest.mark.asyncio
async def test_fetch_metadata_rejects_redirect():
    def handler(request):
        return httpx.Response(302, headers={"location": "https://evil.example.com/x"})

    client, _ = client_for(handler)
    async with client:
        result = await fetch_metadata(client, "https://meta.example.com/a.json", timeout=0.2)
    assert result.outcome == FetchOutcome.URI_REJECTED


@pytest.mark.asyncio
async def test_fetch_metadata_rejects_oversized_body():
    def handler(request):
        return httpx.Response(200, content=b"{" + b"x" * 70_000 + b"}")

    client, _ = client_for(handler)
    async with client:
        result = await fetch_metadata(client, "https://meta.example.com/a.json", timeout=0.2)
    assert result.outcome == FetchOutcome.URI_REJECTED


# --- cache ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_metadata_cache_hit_skips_network():
    def handler(request):
        return httpx.Response(200, json={"twitter": "https://x.com/foo"})

    client, transport = client_for(handler)
    cache = MetadataCache(max_entries=10)
    async with client:
        r1 = await fetch_metadata(client, "https://meta.example.com/a.json", timeout=0.2, cache=cache)
        r2 = await fetch_metadata(client, "https://meta.example.com/a.json", timeout=0.2, cache=cache)
    assert transport.calls == 1
    assert r1 == r2


def test_cache_evicts_fifo_when_full():
    cache = MetadataCache(max_entries=2)
    cache.put("a", MetadataResult(FetchOutcome.NO_SOCIALS))
    cache.put("b", MetadataResult(FetchOutcome.NO_SOCIALS))
    cache.put("c", MetadataResult(FetchOutcome.NO_SOCIALS))
    assert cache.get("a") is None
    assert cache.get("b") is not None
    assert cache.get("c") is not None


# --- score_metadata: pure decision table, no network ----------------------


def test_score_has_socials_passes_at_min_1():
    result = MetadataResult(FetchOutcome.HAS_SOCIALS, has_twitter=True)
    verdict = score_metadata(result, CONFIG)
    assert verdict.passed is True
    assert verdict.would_pass is True


def test_score_no_socials_rejected_at_min_1():
    result = MetadataResult(FetchOutcome.NO_SOCIALS)
    verdict = score_metadata(result, CONFIG)
    assert verdict.passed is False
    assert verdict.would_pass is False


def test_score_min_socials_3_requires_all_three():
    strict_config = Tier2FilterConfig(**{**CONFIG.model_dump(), "min_socials": 3})
    two_socials = MetadataResult(FetchOutcome.HAS_SOCIALS, has_twitter=True, has_telegram=True)
    assert score_metadata(two_socials, strict_config).passed is False
    all_three = MetadataResult(
        FetchOutcome.HAS_SOCIALS, has_twitter=True, has_telegram=True, has_website=True
    )
    assert score_metadata(all_three, strict_config).passed is True


def test_score_fetch_failed_fails_open_by_default():
    result = MetadataResult(FetchOutcome.FETCH_FAILED)
    verdict = score_metadata(result, CONFIG)
    assert verdict.passed is True


def test_score_fetch_timeout_fails_open_by_default():
    result = MetadataResult(FetchOutcome.FETCH_TIMEOUT)
    verdict = score_metadata(result, CONFIG)
    assert verdict.passed is True


def test_score_uri_rejected_fails_open_by_default():
    result = MetadataResult(FetchOutcome.URI_REJECTED)
    verdict = score_metadata(result, CONFIG)
    assert verdict.passed is True


def test_score_fail_closed_flips_the_open_cases():
    closed_config = Tier2FilterConfig(**{**CONFIG.model_dump(), "fail_closed": True})
    for outcome in (FetchOutcome.FETCH_FAILED, FetchOutcome.FETCH_TIMEOUT, FetchOutcome.URI_REJECTED):
        verdict = score_metadata(MetadataResult(outcome), closed_config)
        assert verdict.passed is False, outcome


def test_score_observe_mode_never_rejects_but_computes_verdict():
    observe_config = Tier2FilterConfig(**{**CONFIG.model_dump(), "mode": "observe"})
    no_socials = MetadataResult(FetchOutcome.NO_SOCIALS)
    verdict = score_metadata(no_socials, observe_config)
    assert verdict.passed is True
    assert verdict.would_pass is False  # the computed verdict is still carried for logging


def test_config_rejects_invalid_mode():
    with pytest.raises(ValueError):
        Tier2FilterConfig(**{**CONFIG.model_dump(), "mode": "bogus"})


# --- Tier2Filter.evaluate end to end ---------------------------------------


@pytest.mark.asyncio
async def test_tier2_filter_evaluate_end_to_end():
    def handler(request):
        return httpx.Response(200, json={"telegram": "https://t.me/foo"})

    client, _ = client_for(handler)
    async with client:
        f = Tier2Filter(CONFIG, client)
        result = await f.evaluate(make_candidate())
    assert result.outcome == FetchOutcome.HAS_SOCIALS
    assert result.passed is True
