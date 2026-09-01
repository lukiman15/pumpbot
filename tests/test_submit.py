import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer

from pumpbot.config import ExecutionConfig
from pumpbot.submit import (
    BlockhashExpiredError,
    ConfirmationTimeoutError,
    OnChainFailureError,
    SubmissionError,
    confirm_transaction,
    get_recent_blockhash,
    send_and_confirm,
    send_transaction,
    sign_transaction,
)

# A throwaway keypair generated locally for these tests -- never funded,
# never used against mainnet, exists only so signing logic has a real key
# to sign with.
TEST_KEYPAIR = Keypair()
FAKE_BLOCKHASH = Hash.default()
FAKE_SIGNATURE = "5" * 88  # signature-shaped placeholder, never a real one
LAST_VALID_BLOCK_HEIGHT = 100


def make_transfer_ix(keypair: Keypair) -> list:
    return [
        transfer(
            TransferParams(
                from_pubkey=keypair.pubkey(),
                to_pubkey=Pubkey.new_unique(),
                lamports=1,
            )
        )
    ]


def make_execution_config(**overrides) -> ExecutionConfig:
    defaults = dict(
        skip_preflight=True,
        confirm_poll_interval_seconds=0,
        confirm_timeout_seconds=5,
        max_resubmit_attempts=2,
        ata_close_max_retries=10,
        jito_bundle_enabled=False,
    )
    defaults.update(overrides)
    return ExecutionConfig(**defaults)


class _FakeRpc:
    """Scripted RPC: `responses` maps method name to either a fixed value or
    a callable(call_count_for_method) -> value, so getSignatureStatuses can
    return a different status across successive polls without a real
    network call."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[tuple[str, list]] = []
        self._per_method_count: dict[str, int] = {}

    async def call(self, method, params):
        self.calls.append((method, params))
        self._per_method_count[method] = self._per_method_count.get(method, 0) + 1
        response = self.responses[method]
        if callable(response):
            return response(self._per_method_count[method])
        return response


def latest_blockhash_response(
    blockhash: str = str(Hash.default()), last_valid_block_height: int = LAST_VALID_BLOCK_HEIGHT
):
    return {"value": {"blockhash": blockhash, "lastValidBlockHeight": last_valid_block_height}}


def signature_status(err=None, confirmation_status="confirmed"):
    return {"value": [{"err": err, "confirmationStatus": confirmation_status}]}


def not_yet_landed():
    return {"value": [None]}


@pytest.mark.asyncio
async def test_get_recent_blockhash_parses_response():
    rpc = _FakeRpc({"getLatestBlockhash": latest_blockhash_response()})
    blockhash, last_valid_height = await get_recent_blockhash(rpc)
    assert isinstance(blockhash, Hash)
    assert last_valid_height == LAST_VALID_BLOCK_HEIGHT


def test_sign_transaction_signs_with_the_given_keypair_as_payer():
    ix = make_transfer_ix(TEST_KEYPAIR)
    tx = sign_transaction(ix, TEST_KEYPAIR, FAKE_BLOCKHASH)
    assert tx.message.account_keys[0] == TEST_KEYPAIR.pubkey()
    assert len(tx.signatures) == 1
    assert tx.signatures[0] != type(tx.signatures[0]).default()


@pytest.mark.asyncio
async def test_send_transaction_returns_the_signature():
    rpc = _FakeRpc({"sendTransaction": FAKE_SIGNATURE})
    tx = sign_transaction(make_transfer_ix(TEST_KEYPAIR), TEST_KEYPAIR, FAKE_BLOCKHASH)
    signature = await send_transaction(rpc, tx, skip_preflight=True)
    assert signature == FAKE_SIGNATURE
    method, params = rpc.calls[0]
    assert params[1]["skipPreflight"] is True


@pytest.mark.asyncio
async def test_send_transaction_wraps_rpc_errors_as_submission_error():
    class _RaisingRpc:
        async def call(self, method, params):
            raise RuntimeError("boom")

    tx = sign_transaction(make_transfer_ix(TEST_KEYPAIR), TEST_KEYPAIR, FAKE_BLOCKHASH)
    with pytest.raises(SubmissionError):
        await send_transaction(_RaisingRpc(), tx, skip_preflight=True)


@pytest.mark.asyncio
async def test_confirm_transaction_returns_on_confirmed_status():
    rpc = _FakeRpc({"getSignatureStatuses": signature_status()})
    await confirm_transaction(
        rpc, FAKE_SIGNATURE, LAST_VALID_BLOCK_HEIGHT, poll_interval_seconds=0, timeout_seconds=5
    )


@pytest.mark.asyncio
async def test_confirm_transaction_raises_on_chain_failure_immediately():
    rpc = _FakeRpc({"getSignatureStatuses": signature_status(err={"InstructionError": [0, "boom"]})})
    with pytest.raises(OnChainFailureError):
        await confirm_transaction(
            rpc, FAKE_SIGNATURE, LAST_VALID_BLOCK_HEIGHT, poll_interval_seconds=0, timeout_seconds=5
        )
    # Must not keep polling a transaction that already failed on-chain, and
    # must not even bother checking blockhash expiry once it's already failed.
    assert len(rpc.calls) == 1


@pytest.mark.asyncio
async def test_confirm_transaction_raises_timeout_when_blockhash_still_valid():
    rpc = _FakeRpc(
        {
            "getSignatureStatuses": not_yet_landed(),
            # Still well within validity -- current height < last_valid_block_height.
            "getBlockHeight": LAST_VALID_BLOCK_HEIGHT - 1,
        }
    )
    with pytest.raises(ConfirmationTimeoutError):
        await confirm_transaction(
            rpc, FAKE_SIGNATURE, LAST_VALID_BLOCK_HEIGHT, poll_interval_seconds=0, timeout_seconds=0
        )


@pytest.mark.asyncio
async def test_confirm_transaction_raises_blockhash_expired_before_timeout():
    # Deadline hasn't hit (long timeout), but the cluster has already moved
    # past last_valid_block_height -- must report expiry, not just keep
    # waiting for a timeout that would take much longer to arrive.
    rpc = _FakeRpc(
        {
            "getSignatureStatuses": not_yet_landed(),
            "getBlockHeight": LAST_VALID_BLOCK_HEIGHT + 1,
        }
    )
    with pytest.raises(BlockhashExpiredError):
        await confirm_transaction(
            rpc, FAKE_SIGNATURE, LAST_VALID_BLOCK_HEIGHT, poll_interval_seconds=0, timeout_seconds=999
        )


@pytest.mark.asyncio
async def test_confirm_transaction_polls_until_confirmed():
    def status_sequence(call_count):
        if call_count < 3:
            return not_yet_landed()
        return signature_status()

    rpc = _FakeRpc(
        {
            "getSignatureStatuses": status_sequence,
            "getBlockHeight": LAST_VALID_BLOCK_HEIGHT - 1,
        }
    )
    await confirm_transaction(
        rpc, FAKE_SIGNATURE, LAST_VALID_BLOCK_HEIGHT, poll_interval_seconds=0, timeout_seconds=5
    )
    assert rpc._per_method_count["getSignatureStatuses"] == 3


@pytest.mark.asyncio
async def test_send_and_confirm_end_to_end_returns_signature():
    rpc = _FakeRpc(
        {
            "getLatestBlockhash": latest_blockhash_response(),
            "sendTransaction": FAKE_SIGNATURE,
            "getSignatureStatuses": signature_status(),
        }
    )
    signature = await send_and_confirm(
        rpc, TEST_KEYPAIR, make_transfer_ix(TEST_KEYPAIR), make_execution_config()
    )
    assert signature == FAKE_SIGNATURE
    assert rpc._per_method_count["sendTransaction"] == 1


@pytest.mark.asyncio
async def test_send_and_confirm_propagates_on_chain_failure():
    rpc = _FakeRpc(
        {
            "getLatestBlockhash": latest_blockhash_response(),
            "sendTransaction": FAKE_SIGNATURE,
            "getSignatureStatuses": signature_status(err={"InstructionError": [0, "boom"]}),
        }
    )
    with pytest.raises(OnChainFailureError):
        await send_and_confirm(
            rpc, TEST_KEYPAIR, make_transfer_ix(TEST_KEYPAIR), make_execution_config()
        )


@pytest.mark.asyncio
async def test_send_and_confirm_resubmits_once_after_blockhash_expiry_then_confirms():
    def status_sequence(call_count):
        # First send's poll: never lands. Its confirm_transaction call
        # sees getBlockHeight > last_valid_block_height on its very first
        # check (call_count == 1) and raises BlockhashExpiredError.
        if call_count == 1:
            return not_yet_landed()
        # Second send (after resubmission): confirms immediately.
        return signature_status()

    def block_height_sequence(call_count):
        # Expired for the first send's single check; irrelevant afterwards
        # since the second send confirms before needing another check.
        return LAST_VALID_BLOCK_HEIGHT + 1

    rpc = _FakeRpc(
        {
            "getLatestBlockhash": latest_blockhash_response(),
            "sendTransaction": FAKE_SIGNATURE,
            "getSignatureStatuses": status_sequence,
            "getBlockHeight": block_height_sequence,
        }
    )
    signature = await send_and_confirm(
        rpc, TEST_KEYPAIR, make_transfer_ix(TEST_KEYPAIR), make_execution_config()
    )
    assert signature == FAKE_SIGNATURE
    # One sendTransaction for the original attempt, one for the resubmit.
    assert rpc._per_method_count["sendTransaction"] == 2
    assert rpc._per_method_count["getLatestBlockhash"] == 2


@pytest.mark.asyncio
async def test_send_and_confirm_raises_blockhash_expired_after_exhausting_resubmits():
    rpc = _FakeRpc(
        {
            "getLatestBlockhash": latest_blockhash_response(),
            "sendTransaction": FAKE_SIGNATURE,
            "getSignatureStatuses": not_yet_landed(),
            "getBlockHeight": LAST_VALID_BLOCK_HEIGHT + 1,
        }
    )
    with pytest.raises(BlockhashExpiredError):
        await send_and_confirm(
            rpc,
            TEST_KEYPAIR,
            make_transfer_ix(TEST_KEYPAIR),
            make_execution_config(max_resubmit_attempts=2),
        )
    # Initial attempt + 2 resubmits = 3 total sends, then gives up.
    assert rpc._per_method_count["sendTransaction"] == 3
