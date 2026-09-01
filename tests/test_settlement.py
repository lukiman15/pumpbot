import pytest

from pumpbot import settlement as settlement_module
from pumpbot.settlement import (
    Settlement,
    SettlementMismatchError,
    SettlementUnavailableError,
    settle,
)


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch):
    # Real retries sleep SETTLEMENT_RETRY_DELAY_SECONDS -- zero it so tests
    # exercising the retry path don't pay real wall-clock time for it.
    monkeypatch.setattr(settlement_module, "SETTLEMENT_RETRY_DELAY_SECONDS", 0.0)

WALLET = "97K6nsFhBWDKwQf6heDDhDtsCRC4779LPHSFZkc2zqK4"
OTHER = "6M4YnRkeyWH89coGFHJjC4yALemaB18eCNnkEaU1T2p7"


def make_tx(fee_payer: str, pre: int, post: int, fee: int = 5000, slot: int = 12345) -> dict:
    return {
        "slot": slot,
        "transaction": {"message": {"accountKeys": [fee_payer, OTHER]}},
        "meta": {"fee": fee, "preBalances": [pre, 0], "postBalances": [post, 0]},
    }


class FakeRpc:
    def __init__(self, results: list):
        self._results = list(results)
        self.calls = []

    async def call(self, method, params=None, pool="trading"):
        self.calls.append((method, params, pool))
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.mark.asyncio
async def test_settle_extracts_fee_and_delta():
    tx = make_tx(WALLET, pre=10_000_000, post=5_074_142, fee=5000, slot=42)
    rpc = FakeRpc([tx])

    settlement = await settle(rpc, "sig1", WALLET, "BUY")

    assert isinstance(settlement, Settlement)
    assert settlement.fee_lamports == 5000
    assert settlement.sol_delta_lamports == 5_074_142 - 10_000_000
    assert settlement.slot == 42
    assert settlement.leg_kind == "BUY"


@pytest.mark.asyncio
async def test_settle_asserts_fee_payer_at_index_0():
    tx = make_tx(OTHER, pre=1000, post=900)  # wallet is NOT accountKeys[0]
    rpc = FakeRpc([tx])

    with pytest.raises(SettlementMismatchError):
        await settle(rpc, "sig1", WALLET, "SELL")


@pytest.mark.asyncio
async def test_settle_raises_unavailable_when_transaction_is_null_then_retries():
    tx = make_tx(WALLET, pre=1000, post=2000)
    rpc = FakeRpc([None, None, tx])  # unavailable twice, then succeeds on the 3rd (final) try

    settlement = await settle(rpc, "sig1", WALLET, "SELL")
    assert settlement.sol_delta_lamports == 1000
    assert len(rpc.calls) == 3


@pytest.mark.asyncio
async def test_settle_gives_up_after_max_retries():
    rpc = FakeRpc([None, None, None])

    with pytest.raises(SettlementUnavailableError):
        await settle(rpc, "sig1", WALLET, "SELL")
    assert len(rpc.calls) == 3


@pytest.mark.asyncio
async def test_settle_ata_close_leg_kind():
    tx = make_tx(WALLET, pre=100, post=2_100)
    rpc = FakeRpc([tx])

    settlement = await settle(rpc, "sig1", WALLET, "ATA_CLOSE")
    assert settlement.leg_kind == "ATA_CLOSE"
    assert settlement.sol_delta_lamports == 2000


@pytest.mark.asyncio
async def test_settle_uses_default_trading_pool():
    tx = make_tx(WALLET, pre=100, post=200)
    rpc = FakeRpc([tx])

    await settle(rpc, "sig1", WALLET, "BUY")
    assert rpc.calls[0][2] == "trading"


def test_settlement_to_dict_shape():
    s = Settlement(fee_lamports=5000, sol_delta_lamports=-123, slot=7, leg_kind="BUY")
    assert s.to_dict() == {
        "fee_lamports": 5000,
        "sol_delta_lamports": -123,
        "slot": 7,
        "leg_kind": "BUY",
    }
