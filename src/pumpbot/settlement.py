"""Reads real on-chain fee and SOL delta back from a confirmed signature.

This is where the ledger's load-bearing decision lives: realized PnL and
fees are read back from the chain via getTransaction, never modeled from
config.yaml -- modeling them would reproduce exactly the unvalidated
estimates the PRD flags as unreliable. A settlement failure must never
affect trading: this is a read-only afterthought on an already-confirmed
trade, called only after send_and_confirm has already returned
successfully.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from typing import Any

from pumpbot.rpc import RpcClient

SETTLEMENT_RETRY_DELAY_SECONDS = 1.0
SETTLEMENT_MAX_RETRIES = 2


class SettlementUnavailableError(RuntimeError):
    """getTransaction returned null for this signature -- an expected,
    retryable RPC-propagation lag right after confirmation, not a bug."""


class SettlementMismatchError(RuntimeError):
    """accountKeys[0] is not the wallet -- the fee-payer-at-index-0
    assumption sol_delta_lamports relies on (see the plan's Accounting
    Rules) doesn't hold for this transaction. A real bug: must be loud
    rather than silently mis-attributing someone else's balance delta."""


@dataclass(frozen=True)
class Settlement:
    fee_lamports: int
    sol_delta_lamports: int
    slot: int
    leg_kind: str  # "BUY" | "SELL" | "ATA_CLOSE"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def _fetch_settlement(
    rpc: RpcClient, signature: str, wallet_pubkey: str, leg_kind: str
) -> Settlement:
    tx = await rpc.call(
        "getTransaction",
        [
            signature,
            {
                "encoding": "json",
                "commitment": "confirmed",
                "maxSupportedTransactionVersion": 0,
            },
        ],
    )
    if tx is None:
        raise SettlementUnavailableError(f"getTransaction returned null for {signature}")

    account_keys = tx["transaction"]["message"]["accountKeys"]
    actual_fee_payer = account_keys[0] if account_keys else None
    if actual_fee_payer != wallet_pubkey:
        raise SettlementMismatchError(
            f"signature {signature}: accountKeys[0]={actual_fee_payer!r} != "
            f"wallet {wallet_pubkey!r}"
        )

    meta = tx["meta"]
    sol_delta_lamports = meta["postBalances"][0] - meta["preBalances"][0]
    return Settlement(
        fee_lamports=meta["fee"],
        sol_delta_lamports=sol_delta_lamports,
        slot=tx["slot"],
        leg_kind=leg_kind,
    )


async def settle(rpc: RpcClient, signature: str, wallet_pubkey: str, leg_kind: str) -> Settlement:
    """Retries SettlementUnavailableError up to SETTLEMENT_MAX_RETRIES times
    with a short delay -- getTransaction can lag a few hundred ms behind
    the confirmation this is called right after. Lets SettlementMismatchError
    propagate immediately: it's a real bug, not a transient condition."""
    last_exc: SettlementUnavailableError | None = None
    for attempt in range(SETTLEMENT_MAX_RETRIES + 1):
        try:
            return await _fetch_settlement(rpc, signature, wallet_pubkey, leg_kind)
        except SettlementUnavailableError as exc:
            last_exc = exc
            if attempt < SETTLEMENT_MAX_RETRIES:
                await asyncio.sleep(SETTLEMENT_RETRY_DELAY_SECONDS)
    assert last_exc is not None
    raise last_exc
