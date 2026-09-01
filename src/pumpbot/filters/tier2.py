"""Tier-2 filter: social-metadata gate.

Every PumpPortal create event carries a `uri` pointing at the token's
metadata JSON, which declares whether the launch advertises a Twitter,
Telegram, or website. Published survival analysis correlates that
declared presence with graduation rate. This is the first filter in the
project that needs I/O (an HTTP fetch of a creator-supplied URL), so it
lives here rather than in tier1.py -- that module's contract is pure
pass/reject with no RPC or I/O, and an HTTP fetch would break it.

The signal is declared presence, not verified reachability -- the cited
research measured whether a launch advertises a channel, not whether the
link resolves or the account is real. Do not extend this to fetch,
resolve, or validate the URLs themselves.

fetch_metadata() treats `uri` as attacker-controlled (the creator chooses
it): https-only, no redirects followed, response body capped at 64 KB,
parsed as JSON only, no credentials/cookies/custom headers ever sent, and
the response body is never logged -- only the extracted boolean flags and
host.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit

import httpx

from pumpbot.config import Tier2FilterConfig
from pumpbot.filters.tier1 import Candidate

logger = logging.getLogger(__name__)

MAX_METADATA_BYTES = 64 * 1024


class MetadataRejectedError(RuntimeError):
    """A hostile-input guard tripped: non-https scheme, a redirect
    response, or a body exceeding the 64 KB cap. Kept distinct from
    httpx's own exceptions (real network failures) and asyncio.TimeoutError
    (the fetch_timeout_seconds budget) so each is independently testable."""


class FetchOutcome(str, Enum):
    HAS_SOCIALS = "has_socials"
    NO_SOCIALS = "no_socials"
    FETCH_FAILED = "fetch_failed"
    FETCH_TIMEOUT = "fetch_timeout"
    URI_REJECTED = "uri_rejected"


@dataclass(frozen=True)
class MetadataResult:
    outcome: FetchOutcome
    has_twitter: bool = False
    has_telegram: bool = False
    has_website: bool = False


@dataclass(frozen=True)
class Tier2Result:
    passed: bool
    # The gate's verdict before mode is applied -- in observe mode `passed`
    # is always True, but `would_pass` is still carried through for logging
    # so the gate can be validated against Milestone 2's ledger without a
    # code change.
    would_pass: bool
    outcome: FetchOutcome
    has_twitter: bool
    has_telegram: bool
    has_website: bool


class MetadataCache:
    """Bounded FIFO cache keyed by uri. Distinct mints regularly reuse a
    metadata URI, and a repeat fetch is pure cost."""

    def __init__(self, max_entries: int) -> None:
        self._max_entries = max_entries
        self._data: dict[str, MetadataResult] = {}

    def get(self, uri: str) -> MetadataResult | None:
        return self._data.get(uri)

    def put(self, uri: str, result: MetadataResult) -> None:
        if uri in self._data:
            return
        if self._max_entries <= 0:
            return
        if len(self._data) >= self._max_entries:
            oldest = next(iter(self._data))
            del self._data[oldest]
        self._data[uri] = result


async def _fetch_raw_body(client: httpx.AsyncClient, uri: str, timeout: float) -> bytes:
    """Raises MetadataRejectedError for scheme/redirect/size guard
    violations, httpx.HTTPError for network failures, or lets
    asyncio.TimeoutError propagate for the deadline. Never sends
    credentials, cookies, or custom headers."""
    scheme = urlsplit(uri).scheme.lower()
    if scheme != "https":
        raise MetadataRejectedError(f"non-https scheme: {scheme!r}")

    async with asyncio.timeout(timeout):
        async with client.stream("GET", uri, follow_redirects=False) as resp:
            if 300 <= resp.status_code < 400:
                raise MetadataRejectedError(f"redirect response: {resp.status_code}")
            body = bytearray()
            async for chunk in resp.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_METADATA_BYTES:
                    raise MetadataRejectedError("response body exceeded 64 KB cap")
            return bytes(body)


async def fetch_metadata(
    client: httpx.AsyncClient,
    uri: str,
    timeout: float,
    cache: MetadataCache | None = None,
) -> MetadataResult:
    """Fetches and scores a candidate's metadata `uri`. Always returns a
    MetadataResult -- never raises -- with exactly one of the five outcome
    categories. Cache hits skip the fetch entirely."""
    if cache is not None:
        cached = cache.get(uri)
        if cached is not None:
            return cached

    host = urlsplit(uri).hostname or ""
    try:
        body = await _fetch_raw_body(client, uri, timeout)
    except MetadataRejectedError as exc:
        logger.info("tier2 fetch outcome=uri_rejected host=%s reason=%s", host, exc)
        result = MetadataResult(FetchOutcome.URI_REJECTED)
    except TimeoutError:
        logger.info("tier2 fetch outcome=fetch_timeout host=%s", host)
        result = MetadataResult(FetchOutcome.FETCH_TIMEOUT)
    except httpx.HTTPError as exc:
        logger.info("tier2 fetch outcome=fetch_failed host=%s reason=%s", host, exc)
        result = MetadataResult(FetchOutcome.FETCH_FAILED)
    else:
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = None
        if not isinstance(data, dict):
            logger.info("tier2 fetch outcome=fetch_failed host=%s reason=invalid_json", host)
            result = MetadataResult(FetchOutcome.FETCH_FAILED)
        else:
            has_twitter = bool(data.get("twitter"))
            has_telegram = bool(data.get("telegram"))
            has_website = bool(data.get("website"))
            outcome = (
                FetchOutcome.HAS_SOCIALS
                if (has_twitter or has_telegram or has_website)
                else FetchOutcome.NO_SOCIALS
            )
            logger.info(
                "tier2 fetch outcome=%s host=%s twitter=%s telegram=%s website=%s",
                outcome.value, host, has_twitter, has_telegram, has_website,
            )
            result = MetadataResult(outcome, has_twitter, has_telegram, has_website)

    if cache is not None:
        cache.put(uri, result)
    return result


def score_metadata(result: MetadataResult, config: Tier2FilterConfig) -> Tier2Result:
    """Pure decision table -- no network involved, so every outcome
    category is directly unit-testable. Kept separate from fetch_metadata
    the same way tier1.py separates its pure evaluate() from any I/O."""
    socials_present = sum([result.has_twitter, result.has_telegram, result.has_website])

    if result.outcome in (FetchOutcome.HAS_SOCIALS, FetchOutcome.NO_SOCIALS):
        would_pass = socials_present >= config.min_socials
    else:
        # fetch_failed / fetch_timeout / uri_rejected: fail OPEN by default
        # so a gateway outage degrades the bot toward its current behavior
        # rather than silently halting all trading. fail_closed inverts
        # this for an operator who prefers the opposite.
        would_pass = not config.fail_closed

    passed = True if config.mode == "observe" else would_pass

    return Tier2Result(
        passed=passed,
        would_pass=would_pass,
        outcome=result.outcome,
        has_twitter=result.has_twitter,
        has_telegram=result.has_telegram,
        has_website=result.has_website,
    )


class Tier2Filter:
    def __init__(self, config: Tier2FilterConfig, http_client: httpx.AsyncClient) -> None:
        self._config = config
        self._client = http_client
        self._cache = MetadataCache(config.cache_max_entries)

    async def evaluate(self, candidate: Candidate) -> Tier2Result:
        metadata = await fetch_metadata(
            self._client, candidate.uri, self._config.fetch_timeout_seconds, self._cache
        )
        return score_metadata(metadata, self._config)
