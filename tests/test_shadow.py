import time

import pytest

from pumpbot.config import ShadowConfig
from pumpbot.ledger import ShadowPrice
from pumpbot.shadow import EXCLUDED_REJECTED_REASONS, ShadowTracker

# A real, well-formed base58 pubkey -- _fetch_curve round-trips it through
# solders.pubkey.Pubkey.from_string, which rejects an arbitrary short string.
MINT = "EczjCg25qBFUYRACbijFTyB3k1kDbueYRcjnN6oFpump"

ENABLED_CONFIG = ShadowConfig(
    enabled=True,
    sample_fraction=1.0,
    poll_interval_seconds=15.0,
    horizon_seconds=300.0,
    max_tracked=50,
)


class FakeLedger:
    def __init__(self):
        self.appended: list = []

    def append(self, event) -> None:
        self.appended.append(event)


class FakeRpc:
    """Returns curves from a queue keyed by call order; records pool used."""

    def __init__(self, curve_infos: list):
        self._infos = list(curve_infos)
        self.pools_used: list[str] = []

    async def call(self, method, params=None, pool="trading"):
        self.pools_used.append(pool)
        return self._infos.pop(0)


def curve_info(virtual_sol: int, virtual_tokens: int, complete: bool = False) -> dict:
    import base64
    import struct

    header = struct.Struct("<8s5QB")
    raw = header.pack(
        b"\x00" * 8, virtual_tokens, virtual_sol, 0, 0, 1_000_000_000_000_000, complete
    )
    return {"value": {"data": [base64.b64encode(raw).decode(), "base64"]}}


def missing_curve_info() -> dict:
    return {"value": None}


# --- sampling ----------------------------------------------------------


def test_track_admits_when_sample_draw_passes():
    ledger = FakeLedger()
    rpc = FakeRpc([])
    config = ShadowConfig(**{**ENABLED_CONFIG.model_dump(), "sample_fraction": 0.5})
    tracker = ShadowTracker(config, rpc, ledger, rng=lambda: 0.1)  # 0.1 < 0.5 -> admitted

    tracker.track(MINT, arm="rejected", reject_reason="creator_supply_too_high")
    assert tracker.tracked_count == 1


def test_track_rejects_when_sample_draw_fails():
    ledger = FakeLedger()
    rpc = FakeRpc([])
    config = ShadowConfig(**{**ENABLED_CONFIG.model_dump(), "sample_fraction": 0.5})
    tracker = ShadowTracker(config, rpc, ledger, rng=lambda: 0.9)  # 0.9 >= 0.5 -> not admitted

    tracker.track(MINT, arm="rejected", reject_reason="creator_supply_too_high")
    assert tracker.tracked_count == 0


def test_bought_arm_always_sampled_at_1_regardless_of_sample_fraction():
    ledger = FakeLedger()
    rpc = FakeRpc([])
    config = ShadowConfig(**{**ENABLED_CONFIG.model_dump(), "sample_fraction": 0.01})
    tracker = ShadowTracker(config, rpc, ledger, rng=lambda: 0.99)  # would fail any real draw

    tracker.track(MINT, arm="bought", trade_id="trade-1")
    assert tracker.tracked_count == 1


def test_mayhem_mode_excluded_from_rejected_arm_sampling():
    ledger = FakeLedger()
    rpc = FakeRpc([])
    tracker = ShadowTracker(ENABLED_CONFIG, rpc, ledger, rng=lambda: 0.0)

    assert "mayhem_mode_unauthorized" in EXCLUDED_REJECTED_REASONS
    tracker.track(MINT, arm="rejected", reject_reason="mayhem_mode_unauthorized")
    assert tracker.tracked_count == 0


def test_disabled_tracker_never_admits():
    ledger = FakeLedger()
    rpc = FakeRpc([])
    config = ShadowConfig(**{**ENABLED_CONFIG.model_dump(), "enabled": False})
    tracker = ShadowTracker(config, rpc, ledger, rng=lambda: 0.0)

    tracker.track(MINT, arm="bought", trade_id="t1")
    assert tracker.tracked_count == 0


def test_track_is_idempotent_for_already_tracked_mint():
    ledger = FakeLedger()
    rpc = FakeRpc([])
    tracker = ShadowTracker(ENABLED_CONFIG, rpc, ledger, rng=lambda: 0.0)

    tracker.track(MINT, arm="bought", trade_id="t1")
    tracker.track(MINT, arm="bought", trade_id="t1")
    assert tracker.tracked_count == 1


# --- bounded capacity ----------------------------------------------------


def test_max_tracked_enforced():
    ledger = FakeLedger()
    rpc = FakeRpc([])
    config = ShadowConfig(**{**ENABLED_CONFIG.model_dump(), "max_tracked": 2})
    tracker = ShadowTracker(config, rpc, ledger, rng=lambda: 0.0)

    tracker.track(MINT, arm="bought", trade_id="t1")
    tracker.track("MintB", arm="bought", trade_id="t2")
    tracker.track("MintC", arm="bought", trade_id="t3")

    assert tracker.tracked_count == 2


# --- polling: horizon expiry, curve-complete termination, missing account -


@pytest.mark.asyncio
async def test_poll_appends_shadow_price_and_computes_return():
    ledger = FakeLedger()
    rpc = FakeRpc([curve_info(30_000_000_000, 1_073_000_000_000_000)])
    tracker = ShadowTracker(ENABLED_CONFIG, rpc, ledger, rng=lambda: 0.0)
    tracker.track(MINT, arm="bought", trade_id="t1")

    await tracker._poll_one(MINT)

    assert len(ledger.appended) == 1
    event = ledger.appended[0]
    assert isinstance(event, ShadowPrice)
    assert event.mint == MINT
    assert event.arm == "bought"
    assert event.curve_complete is False
    # first poll -- return_from_first_seen is 0.0 against itself
    assert event.return_from_first_seen == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_poll_skips_and_keeps_tracking_when_curve_account_missing():
    ledger = FakeLedger()
    rpc = FakeRpc([missing_curve_info()])
    tracker = ShadowTracker(ENABLED_CONFIG, rpc, ledger, rng=lambda: 0.0)
    tracker.track(MINT, arm="bought", trade_id="t1")

    await tracker._poll_one(MINT)

    assert ledger.appended == []
    assert tracker.tracked_count == 1  # still tracked, not dropped


@pytest.mark.asyncio
async def test_poll_stops_tracking_when_curve_complete():
    ledger = FakeLedger()
    rpc = FakeRpc([curve_info(90_000_000_000, 500_000_000_000_000, complete=True)])
    tracker = ShadowTracker(ENABLED_CONFIG, rpc, ledger, rng=lambda: 0.0)
    tracker.track(MINT, arm="bought", trade_id="t1")

    await tracker._poll_one(MINT)

    assert len(ledger.appended) == 1
    assert ledger.appended[0].curve_complete is True
    assert tracker.tracked_count == 0  # a graduated token is a result, tracking stops


@pytest.mark.asyncio
async def test_poll_stops_tracking_after_horizon_elapsed():
    ledger = FakeLedger()
    rpc = FakeRpc([curve_info(30_000_000_000, 1_073_000_000_000_000)])
    config = ShadowConfig(**{**ENABLED_CONFIG.model_dump(), "horizon_seconds": 10.0})
    tracker = ShadowTracker(config, rpc, ledger, rng=lambda: 0.0)
    tracker.track(MINT, arm="bought", trade_id="t1")
    tracker._tracked[MINT].started_at = time.monotonic() - 11.0  # already past horizon

    await tracker._poll_one(MINT)

    assert len(ledger.appended) == 1
    assert tracker.tracked_count == 0


@pytest.mark.asyncio
async def test_poll_uses_shadow_pool_not_trading():
    ledger = FakeLedger()
    rpc = FakeRpc([curve_info(30_000_000_000, 1_073_000_000_000_000)])
    tracker = ShadowTracker(ENABLED_CONFIG, rpc, ledger, rng=lambda: 0.0)
    tracker.track(MINT, arm="bought", trade_id="t1")

    await tracker._poll_one(MINT)

    assert rpc.pools_used == ["shadow"]


@pytest.mark.asyncio
async def test_poll_on_untracked_mint_is_a_noop():
    ledger = FakeLedger()
    rpc = FakeRpc([])
    tracker = ShadowTracker(ENABLED_CONFIG, rpc, ledger, rng=lambda: 0.0)

    await tracker._poll_one("NeverTracked")  # should not raise, no RPC call made
    assert ledger.appended == []
