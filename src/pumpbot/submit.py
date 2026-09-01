"""Real transaction signing, submission, and confirmation-polling.

Everything else in this project (executor.py's instruction builders,
main.py's `_simulate`) only ever builds and simulates transactions --
nothing before this module has ever signed or broadcast one. This is that
missing piece: fetch a real recent blockhash, sign with the burner
wallet's keypair, submit via `sendTransaction`, and poll
`getSignatureStatuses` until confirmed, failed, or timed out.

Scope boundary, deliberate: this module can move real SOL on mainnet.
Nothing in main.py's current DRY_RUN=true path calls it -- it exists as a
tested, ready building block for whoever operates this bot to wire in and
flip DRY_RUN=false themselves, not something this codebase invokes on its
own. `main()` still refuses to start when DRY_RUN=false until that wiring
is done (see main.py's docstring).

Always `confirmed` commitment throughout, never the RPC default of
`finalized` -- see main.py's `_simulate` docstring for why: `finalized`
genuinely takes ~10s+ to reach and this project has no use for waiting
that long anywhere in a fast-moving new-mint trade.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time

from solders.hash import Hash
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import Transaction

from pumpbot.config import ExecutionConfig
from pumpbot.rpc import RpcClient

logger = logging.getLogger(__name__)


class SubmissionError(RuntimeError):
    """Raised when sendTransaction itself errors (rejected before landing --
    not the same as landing and failing on-chain, see confirm_transaction)."""


class OnChainFailureError(RuntimeError):
    """Raised when a transaction landed but failed on-chain (status.err is
    set). The fee was still spent; tokens/SOL beyond the fee were not."""

    def __init__(self, signature: str, err: object) -> None:
        super().__init__(f"transaction {signature} failed on-chain: {err}")
        self.signature = signature
        self.err = err


class ConfirmationTimeoutError(RuntimeError):
    """Raised when a submitted transaction's signature is never observed as
    confirmed within execution.confirm_timeout_seconds. This does NOT mean
    the transaction failed -- it may still land later off a node this
    project isn't polling, or after the timeout window. Callers must
    reconcile real balance/position state before assuming nothing
    happened; never treat this the same as OnChainFailureError."""

    def __init__(self, signature: str, timeout_seconds: float) -> None:
        super().__init__(
            f"transaction {signature} not confirmed within {timeout_seconds}s"
        )
        self.signature = signature


async def get_recent_blockhash(rpc: RpcClient) -> tuple[Hash, int]:
    """Returns (blockhash, last_valid_block_height)."""
    result = await rpc.call("getLatestBlockhash", [{"commitment": "confirmed"}])
    value = result["value"]
    return Hash.from_string(value["blockhash"]), value["lastValidBlockHeight"]


def sign_transaction(
    instructions: list[Instruction],
    keypair: Keypair,
    recent_blockhash: Hash,
) -> Transaction:
    """Builds and fully signs a transaction with `keypair` as both fee
    payer and sole signer. This project has exactly one signer (the burner
    wallet) -- unlike the historical live samples program.py documents,
    which were all CPI'd through another program and could substitute a
    different `user` account, nothing here is ever CPI'd, so fee payer and
    `user` are always the same key."""
    return Transaction.new_signed_with_payer(
        instructions, keypair.pubkey(), [keypair], recent_blockhash
    )


async def send_transaction(rpc: RpcClient, tx: Transaction, skip_preflight: bool) -> str:
    """Submits an already-signed transaction. Returns the signature
    sendTransaction itself hands back -- this is NOT confirmation, call
    confirm_transaction separately. maxRetries=0 is deliberate: this
    module's own poll-and-the-caller's-retry-policy governs retries, not
    the RPC node's opaque internal rebroadcast behavior."""
    raw_b64 = base64.b64encode(bytes(tx)).decode()
    try:
        return await rpc.call(
            "sendTransaction",
            [
                raw_b64,
                {
                    "encoding": "base64",
                    "skipPreflight": skip_preflight,
                    "preflightCommitment": "confirmed",
                    "maxRetries": 0,
                },
            ],
        )
    except Exception as exc:
        raise SubmissionError(f"sendTransaction failed: {exc}") from exc


async def confirm_transaction(
    rpc: RpcClient,
    signature: str,
    poll_interval_seconds: float,
    timeout_seconds: float,
) -> None:
    """Polls getSignatureStatuses until `signature` reaches confirmed (or
    finalized, which implies confirmed) commitment. Raises
    OnChainFailureError immediately if the transaction lands with an
    error (no point continuing to poll a transaction that already failed),
    or ConfirmationTimeoutError if the deadline passes with no status at
    all -- see that error's docstring on why those two are NOT
    interchangeable for a caller."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        result = await rpc.call("getSignatureStatuses", [[signature]])
        status = result["value"][0]
        if status is not None:
            if status.get("err") is not None:
                raise OnChainFailureError(signature, status["err"])
            if status.get("confirmationStatus") in ("confirmed", "finalized"):
                return
        if time.monotonic() >= deadline:
            raise ConfirmationTimeoutError(signature, timeout_seconds)
        await asyncio.sleep(poll_interval_seconds)


async def send_and_confirm(
    rpc: RpcClient,
    keypair: Keypair,
    instructions: list[Instruction],
    execution_config: ExecutionConfig,
) -> str:
    """End-to-end: fetch a real blockhash, sign, submit, poll for
    confirmation. Returns the confirmed signature.

    Raises SubmissionError (rejected before landing -- fee not spent),
    OnChainFailureError (landed but failed -- fee spent, nothing else),
    or ConfirmationTimeoutError (genuinely unknown outcome -- reconcile
    real state before assuming anything). Callers must handle these three
    distinctly; collapsing them into one generic "buy/sell failed" bucket
    would treat "definitely nothing happened beyond a fee" the same as
    "we don't actually know," which is exactly the distinction position
    reconciliation needs."""
    fee_payer = keypair.pubkey()
    blockhash, _last_valid_height = await get_recent_blockhash(rpc)
    tx = sign_transaction(instructions, keypair, blockhash)
    signature = await send_transaction(rpc, tx, execution_config.skip_preflight)
    logger.info("submitted signature=%s fee_payer=%s", signature, fee_payer)
    await confirm_transaction(
        rpc,
        signature,
        execution_config.confirm_poll_interval_seconds,
        execution_config.confirm_timeout_seconds,
    )
    logger.info("confirmed signature=%s", signature)
    return signature
