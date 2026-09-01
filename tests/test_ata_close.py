import pytest
from solders.hash import Hash
from solders.keypair import Keypair

import pumpbot.ata_close as ata_close
from pumpbot.ata_close import close_ata_after_exit, get_token_account_balance_raw
from pumpbot.config import ExecutionConfig
from pumpbot.program import TOKEN_2022_PROGRAM_ID, derive_associated_token_address


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    # Retries in this module deliberately pause for real between attempts
    # in production -- tests don't need to wait 2s per retry to prove the
    # retry logic works.
    monkeypatch.setattr(ata_close, "ATA_CLOSE_RETRY_DELAY_SECONDS", 0)

TEST_KEYPAIR = Keypair()
MINT = Keypair().pubkey()  # arbitrary mint-shaped pubkey, never a real mint
ATA = derive_associated_token_address(TEST_KEYPAIR.pubkey(), MINT, TOKEN_2022_PROGRAM_ID)
FAKE_SIGNATURE = "5" * 88
LAST_VALID_BLOCK_HEIGHT = 100


def make_execution_config(**overrides) -> ExecutionConfig:
    defaults = dict(
        skip_preflight=True,
        confirm_poll_interval_seconds=0,
        confirm_timeout_seconds=0,
        max_resubmit_attempts=0,
        ata_close_max_retries=3,
        jito_bundle_enabled=False,
    )
    defaults.update(overrides)
    return ExecutionConfig(**defaults)


class _FakeRpc:
    """Scripted RPC: `responses` maps method name to a fixed value or a
    callable(call_count_for_method) -> value."""

    def __init__(self, responses: dict):
        self.responses = responses
        self._per_method_count: dict[str, int] = {}

    async def call(self, method, params):
        self._per_method_count[method] = self._per_method_count.get(method, 0) + 1
        response = self.responses[method]
        if callable(response):
            return response(self._per_method_count[method])
        return response

    def count(self, method: str) -> int:
        return self._per_method_count.get(method, 0)


def account_missing():
    return {"value": None}


def account_present():
    return {"value": {"data": ["", "base64"]}}


def token_balance(amount: int):
    return {"value": {"amount": str(amount)}}


def latest_blockhash():
    return {"value": {"blockhash": str(Hash.default()), "lastValidBlockHeight": LAST_VALID_BLOCK_HEIGHT}}


def confirmed_status():
    return {"value": [{"err": None, "confirmationStatus": "confirmed"}]}


@pytest.mark.asyncio
async def test_get_token_account_balance_raw_returns_none_when_missing():
    rpc = _FakeRpc({"getAccountInfo": account_missing()})
    assert await get_token_account_balance_raw(rpc, ATA) is None


@pytest.mark.asyncio
async def test_get_token_account_balance_raw_returns_amount_when_present():
    rpc = _FakeRpc({"getAccountInfo": account_present(), "getTokenAccountBalance": token_balance(0)})
    assert await get_token_account_balance_raw(rpc, ATA) == 0


@pytest.mark.asyncio
async def test_close_ata_after_exit_returns_none_when_already_closed():
    rpc = _FakeRpc({"getAccountInfo": account_missing()})
    result = await close_ata_after_exit(
        rpc, TEST_KEYPAIR, MINT, TOKEN_2022_PROGRAM_ID, make_execution_config()
    )
    assert result is None


@pytest.mark.asyncio
async def test_close_ata_after_exit_closes_immediately_when_balance_zero():
    rpc = _FakeRpc(
        {
            "getAccountInfo": account_present(),
            "getTokenAccountBalance": token_balance(0),
            "getLatestBlockhash": latest_blockhash(),
            "sendTransaction": FAKE_SIGNATURE,
            "getSignatureStatuses": confirmed_status(),
        }
    )
    result = await close_ata_after_exit(
        rpc, TEST_KEYPAIR, MINT, TOKEN_2022_PROGRAM_ID, make_execution_config()
    )
    assert result == FAKE_SIGNATURE
    assert rpc.count("sendTransaction") == 1


@pytest.mark.asyncio
async def test_close_ata_after_exit_retries_until_balance_reaches_zero():
    def balance_sequence(call_count):
        return token_balance(0 if call_count >= 3 else 5)

    rpc = _FakeRpc(
        {
            "getAccountInfo": account_present(),
            "getTokenAccountBalance": balance_sequence,
            "getLatestBlockhash": latest_blockhash(),
            "sendTransaction": FAKE_SIGNATURE,
            "getSignatureStatuses": confirmed_status(),
        }
    )
    result = await close_ata_after_exit(
        rpc,
        TEST_KEYPAIR,
        MINT,
        TOKEN_2022_PROGRAM_ID,
        make_execution_config(ata_close_max_retries=5),
    )
    assert result == FAKE_SIGNATURE
    assert rpc.count("getTokenAccountBalance") == 3
    assert rpc.count("sendTransaction") == 1


@pytest.mark.asyncio
async def test_close_ata_after_exit_gives_up_quietly_when_balance_never_zero():
    rpc = _FakeRpc(
        {
            "getAccountInfo": account_present(),
            "getTokenAccountBalance": token_balance(5),
        }
    )
    result = await close_ata_after_exit(
        rpc,
        TEST_KEYPAIR,
        MINT,
        TOKEN_2022_PROGRAM_ID,
        make_execution_config(ata_close_max_retries=3),
    )
    assert result is None  # never raises -- rent forfeited, not fund-unsafe
    assert rpc.count("getTokenAccountBalance") == 3


@pytest.mark.asyncio
async def test_close_ata_after_exit_gives_up_quietly_when_send_keeps_failing():
    # Zero balance every time (so it always attempts the close), but the
    # send never confirms in time and the blockhash never expires --
    # ConfirmationTimeoutError every attempt.
    rpc = _FakeRpc(
        {
            "getAccountInfo": account_present(),
            "getTokenAccountBalance": token_balance(0),
            "getLatestBlockhash": latest_blockhash(),
            "sendTransaction": FAKE_SIGNATURE,
            "getSignatureStatuses": {"value": [None]},
            "getBlockHeight": LAST_VALID_BLOCK_HEIGHT - 1,
        }
    )
    result = await close_ata_after_exit(
        rpc,
        TEST_KEYPAIR,
        MINT,
        TOKEN_2022_PROGRAM_ID,
        make_execution_config(ata_close_max_retries=2, confirm_timeout_seconds=0),
    )
    assert result is None
    assert rpc.count("sendTransaction") == 2
