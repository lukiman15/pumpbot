"""Real transaction signing, submission, and confirmation-polling.

Everything else in this project (executor.py's instruction builders,
main.py's `_simulate`) only ever builds and simulates transactions --
nothing before this module has ever signed or broadcast one. This is that
missing piece: fetch a real recent blockhash, sign with the burner
wallet's keypair, submit via `sendTransaction`, and poll
`getSignatureStatuses` until confirmed, failed, timed out, or the
blockhash provably expires (in which case it's safe to resubmit with a
fresh one -- see BlockhashExpiredError's docstring for why that specific
condition, and only that one, makes resubmission safe from a double-send).

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

from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import Transaction

from pumpbot.config import ExecutionConfig, FeesConfig
from pumpbot.curve import LAMPORTS_PER_SOL
from pumpbot.rpc import RpcClient

logger = logging.getLogger(__name__)

# Solana's flat per-signature fee. This project has exactly one signer per
# transaction (see sign_transaction's docstring), so this is also the
# per-transaction base fee.
BASE_FEE_LAMPORTS_PER_SIGNATURE = 5000


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
    confirmed within execution.confirm_timeout_seconds, AND its blockhash
    hasn't expired either -- i.e. genuinely unknown, could still land any
    moment. This does NOT mean the transaction failed. Callers must
    reconcile real balance/position state before assuming nothing
    happened; never treat this the same as OnChainFailureError.

    Distinct from BlockhashExpiredError: that one means the transaction
    can PROVABLY never land anymore (safe to resubmit); this one means we
    simply stopped waiting while it could still land (NOT safe to
    resubmit without risking a double-send if both land)."""

    def __init__(self, signature: str, timeout_seconds: float) -> None:
        super().__init__(
            f"transaction {signature} not confirmed within {timeout_seconds}s "
            "and its blockhash has not expired -- it may still land; do not "
            "resubmit without further reconciliation"
        )
        self.signature = signature


class BlockhashExpiredError(RuntimeError):
    """Raised when a transaction's blockhash outlives its
    lastValidBlockHeight before any confirmation status was ever observed
    for it. Solana's runtime rejects any transaction referencing an
    expired blockhash before execution -- once the cluster's own
    (confirmed-commitment) block height has passed lastValidBlockHeight,
    this specific signature is PROVABLY dead and can never land later, no
    matter how long anyone waits. That guarantee (not a guess, not a
    timeout) is what makes resubmitting with a fresh blockhash safe here
    and NOT safe on a plain ConfirmationTimeoutError, where the original
    transaction could still land at any moment and a resubmit risks a
    double-send if both go through."""

    def __init__(self, signature: str, last_valid_block_height: int, current_block_height: int) -> None:
        super().__init__(
            f"transaction {signature} blockhash expired "
            f"(lastValidBlockHeight={last_valid_block_height}, "
            f"current confirmed block height={current_block_height}) -- "
            "this signature can never land, safe to resubmit"
        )
        self.signature = signature


def estimate_entry_fee_lamports(fees_config: FeesConfig, signature_count: int) -> int:
    """Base fee + priority fee for `signature_count` transactions --
    EXCLUDING ATA rent (see MILESTONE-3-HANDOFF.md Section 5.3). Rent is
    paid in the buy leg and recovered in the ATA-close leg -- it's float,
    not a cost, and a fee gate that counted it would reject nearly every
    trade at a small position size while looking like a working filter.

    Used by main.py's fee gate to estimate the cost of a single entry
    (signature_count=1, one bundled create-ATA+buy transaction)."""
    base_fee_lamports = signature_count * BASE_FEE_LAMPORTS_PER_SIGNATURE
    priority_fee_per_tx_lamports = round(fees_config.priority_fee_sol * LAMPORTS_PER_SOL)
    return base_fee_lamports + signature_count * priority_fee_per_tx_lamports


def build_compute_budget_instructions(fees_config: FeesConfig) -> list[Instruction]:
    """Returns the ComputeBudget instructions to prepend to every
    transaction (buy, sell, and ATA close alike) -- see
    MILESTONE-3-HANDOFF.md Section 5.1/5.2.

    Always includes SetComputeUnitLimit: an explicit, tight limit is free
    and improves scheduling versus the default 200,000-CU-per-instruction
    assumption, independent of whether a priority fee is paid at all.

    SetComputeUnitPrice is included only when fees_config.priority_fee_sol
    is nonzero -- 0.0 is this project's deliberate default (it has
    explicitly dropped the latency race; paying for faster inclusion buys
    nothing strategically yet), so the common case emits one instruction,
    not two.

    The two ComputeBudget values are related by:
        priority_fee_lamports = compute_unit_limit * compute_unit_price_micro / 1_000_000
    so achieving a target priority_fee_sol at a fixed compute_unit_limit
    means solving for the price:
        compute_unit_price_micro = priority_fee_lamports * 1_000_000 // compute_unit_limit
    """
    instructions = [set_compute_unit_limit(fees_config.compute_unit_limit)]
    if fees_config.priority_fee_sol > 0.0:
        priority_fee_lamports = round(fees_config.priority_fee_sol * LAMPORTS_PER_SOL)
        compute_unit_price_micro = (
            priority_fee_lamports * 1_000_000 // fees_config.compute_unit_limit
        )
        instructions.append(set_compute_unit_price(compute_unit_price_micro))
    return instructions


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
    last_valid_block_height: int,
    poll_interval_seconds: float,
    timeout_seconds: float,
) -> None:
    """Polls getSignatureStatuses until `signature` reaches confirmed (or
    finalized, which implies confirmed) commitment.

    Raises, in priority order:
    - OnChainFailureError immediately if the transaction lands with an
      error (no point continuing to poll a transaction that already failed).
    - BlockhashExpiredError if the cluster's own confirmed block height has
      passed `last_valid_block_height` with no status ever observed -- this
      signature is now provably dead (see that error's docstring), checked
      BEFORE the timeout deadline so a genuinely-dead transaction is
      reported as such even if the deadline hasn't hit yet.
    - ConfirmationTimeoutError if the deadline passes first, with the
      blockhash still technically valid -- the transaction could still
      land; this is NOT the same guarantee as expiry.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        result = await rpc.call("getSignatureStatuses", [[signature]])
        status = result["value"][0]
        if status is not None:
            if status.get("err") is not None:
                raise OnChainFailureError(signature, status["err"])
            if status.get("confirmationStatus") in ("confirmed", "finalized"):
                return

        current_block_height = await rpc.call("getBlockHeight", [{"commitment": "confirmed"}])
        if current_block_height > last_valid_block_height:
            raise BlockhashExpiredError(signature, last_valid_block_height, current_block_height)

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
    confirmation -- resubmitting with a fresh blockhash if (and only if)
    the previous attempt's blockhash provably expired with no status ever
    observed. Returns the confirmed signature.

    Resubmission is safe specifically because BlockhashExpiredError means
    the PREVIOUS attempt's signature can never land (see that error's
    docstring) -- there is no window where both the old and a new
    submission could land and double-execute the trade. A plain
    ConfirmationTimeoutError does NOT get this treatment: the original
    could still land later, so send_and_confirm gives up rather than risk
    a double-send, and surfaces that error to the caller for reconciliation.

    Raises SubmissionError (rejected before landing -- fee not spent),
    OnChainFailureError (landed but failed -- fee spent, nothing else),
    ConfirmationTimeoutError (genuinely unknown outcome, blockhash still
    live -- reconcile real state before assuming anything), or
    BlockhashExpiredError (only if resubmission attempts are exhausted --
    execution_config.max_resubmit_attempts). Callers must handle these
    distinctly; collapsing them into one generic "buy/sell failed" bucket
    would treat "definitely nothing happened beyond a fee" the same as
    "we don't actually know," which is exactly the distinction position
    reconciliation needs."""
    fee_payer = keypair.pubkey()
    total_attempts = execution_config.max_resubmit_attempts + 1
    for attempt in range(1, total_attempts + 1):
        blockhash, last_valid_height = await get_recent_blockhash(rpc)
        tx = sign_transaction(instructions, keypair, blockhash)
        signature = await send_transaction(rpc, tx, execution_config.skip_preflight)
        logger.info(
            "submitted signature=%s fee_payer=%s attempt=%d/%d",
            signature, fee_payer, attempt, total_attempts,
        )
        try:
            await confirm_transaction(
                rpc,
                signature,
                last_valid_height,
                execution_config.confirm_poll_interval_seconds,
                execution_config.confirm_timeout_seconds,
            )
            logger.info("confirmed signature=%s", signature)
            return signature
        except BlockhashExpiredError:
            if attempt >= total_attempts:
                raise
            logger.warning(
                "signature=%s blockhash expired, resubmitting with a fresh "
                "one (attempt %d/%d) -- safe: the expired signature can "
                "never land",
                signature, attempt + 1, total_attempts,
            )
            continue

    raise AssertionError("unreachable: loop always returns or raises")
