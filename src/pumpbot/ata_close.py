"""ATA close-after-exit policy: recovering rent from a token account once a
position has been fully exited (tokens_remaining reaches 0).

Not fund-critical: SPL Token's CloseAccount instruction refuses on-chain if
the account's token balance is nonzero, so there is no way for this module
to destroy tokens by closing too early -- worst case, closing simply fails
and the wallet forfeits ~0.002 SOL of recoverable rent. That's why this
retries a bounded number of times (execution.ata_close_max_retries) and
then gives up quietly rather than raising -- a failed rent recovery must
never block a position from being considered closed, or halt new trading.

Deliberately a distinct transaction from the final sell, not bundled into
it: bundling would require knowing in advance that the sell empties the
account to EXACTLY zero, but pump.fun's bonding-curve rounding and this
project's own tracked position state (positions.py) can drift by dust from
the real on-chain balance. This module re-checks the real on-chain balance
itself before ever attempting a close, rather than trusting the caller's
belief that the position is fully exited.
"""

from __future__ import annotations

import asyncio
import logging

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from spl.token.instructions import close_account
from spl.token.models import CloseAccountParams

from pumpbot.config import ExecutionConfig, FeesConfig
from pumpbot.program import derive_associated_token_address
from pumpbot.rpc import RpcClient
from pumpbot.submit import (
    BlockhashExpiredError,
    ConfirmationTimeoutError,
    OnChainFailureError,
    SubmissionError,
    build_compute_budget_instructions,
    send_and_confirm,
)

logger = logging.getLogger(__name__)

# Backoff between retries -- independent of submit.py's own confirm-poll
# interval. A failed close attempt (nonzero balance, or a submission
# error) needs a real pause for the underlying condition to have a chance
# of changing, not a single confirmation-poll tick.
ATA_CLOSE_RETRY_DELAY_SECONDS = 2.0


async def get_token_account_balance_raw(rpc: RpcClient, ata: Pubkey) -> int | None:
    """Returns the account's raw token amount, or None if the account
    doesn't exist (already closed, or never created -- e.g. a buy that
    was only ever simulated, never actually sent)."""
    info = await rpc.call(
        "getAccountInfo", [str(ata), {"encoding": "base64", "commitment": "confirmed"}]
    )
    if info["value"] is None:
        return None
    balance = await rpc.call("getTokenAccountBalance", [str(ata), {"commitment": "confirmed"}])
    return int(balance["value"]["amount"])


async def close_ata_after_exit(
    rpc: RpcClient,
    keypair: Keypair,
    mint: Pubkey,
    token_program_id: Pubkey,
    execution_config: ExecutionConfig,
    fees_config: FeesConfig,
) -> str | None:
    """Attempts to close the wallet's ATA for `mint`, recovering its rent.

    Returns the confirmed close signature, or None if there was nothing to
    close (account already gone) or every retry was exhausted (rent left
    unrecovered -- logged, never raised: see module docstring on why this
    must never propagate as a failure to the caller).

    fees_config is used only to size this close's compute-budget
    instructions (see submit.py's build_compute_budget_instructions) --
    an ATA close is never fee-gated (capital/rent recovery beats fee
    discipline; close_fee_reserve_sol exists precisely so a pending close
    is never left unpayable), so fees_config never causes this to reject."""
    ata = derive_associated_token_address(keypair.pubkey(), mint, token_program_id)

    for attempt in range(1, execution_config.ata_close_max_retries + 1):
        try:
            balance = await get_token_account_balance_raw(rpc, ata)
        except Exception:
            logger.exception(
                "ata_close: balance check failed for mint=%s attempt=%d/%d",
                mint, attempt, execution_config.ata_close_max_retries,
            )
            await asyncio.sleep(ATA_CLOSE_RETRY_DELAY_SECONDS)
            continue

        if balance is None:
            logger.info("ata_close: mint=%s already closed (or never created), nothing to do", mint)
            return None

        if balance != 0:
            # Dust left by rounding drift between this project's tracked
            # position state and the real on-chain balance -- see module
            # docstring. CloseAccount would fail on-chain anyway, so this
            # check just saves a doomed round trip; it isn't what makes
            # closing safe (the on-chain program enforces that regardless
            # of what this module checks).
            logger.warning(
                "ata_close: mint=%s balance=%d not yet zero, retrying (%d/%d)",
                mint, balance, attempt, execution_config.ata_close_max_retries,
            )
            await asyncio.sleep(ATA_CLOSE_RETRY_DELAY_SECONDS)
            continue

        close_ix = close_account(
            CloseAccountParams(
                program_id=token_program_id,
                account=ata,
                dest=keypair.pubkey(),
                owner=keypair.pubkey(),
            )
        )
        instructions = [*build_compute_budget_instructions(fees_config), close_ix]
        try:
            signature = await send_and_confirm(rpc, keypair, instructions, execution_config)
        except (SubmissionError, OnChainFailureError, ConfirmationTimeoutError, BlockhashExpiredError) as exc:
            logger.warning(
                "ata_close: close attempt failed mint=%s attempt=%d/%d: %s",
                mint, attempt, execution_config.ata_close_max_retries, exc,
            )
            await asyncio.sleep(ATA_CLOSE_RETRY_DELAY_SECONDS)
            continue
        else:
            logger.info("ata_close: closed mint=%s signature=%s", mint, signature)
            return signature

    logger.error(
        "ata_close: giving up on mint=%s after %d attempts -- rent left "
        "unrecovered, not a fund-safety issue",
        mint, execution_config.ata_close_max_retries,
    )
    return None
