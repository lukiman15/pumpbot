"""Milestone 3, Task 1: verify the one-time-PDA hypothesis
(MILESTONE-3-HANDOFF.md Section 4.2) before anything gets sized from the
single historical trade.

Given a buy signature, fetches it via getTransaction and decomposes the
wallet's lamport balance change in full: base fee, the trade payment
itself (whatever this buy's non-newly-created recipient accounts
received -- bonding_curve + fee_recipient + creator_vault +
buyback_fee_recipient combined), and each account newly created (rent-
funded) in this same transaction, by name, per BUY_ACCOUNTS' verified
ordering (program.py). Specifically checks whether
`user_volume_accumulator` was one of the created accounts -- if so, the
one-time-PDA hypothesis is confirmed and the historical trade's headline
loss is not representative of steady-state per-trade economics.

Read-only. Never signs or sends anything -- getTransaction only.

    uv run python scripts/explain_trade.py <signature>
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import base58
from solders.pubkey import Pubkey

from pumpbot.config import load_settings
from pumpbot.executor import BUY_DISCRIMINATOR
from pumpbot.program import PUMP_FUN_PROGRAM_ID, derive_user_volume_accumulator_pda
from pumpbot.rpc import RpcClient

# Verified BUY_ACCOUNTS ordering -- see program.py's module docstring.
# Do not re-derive; this is load-bearing project history.
BUY_ACCOUNT_NAMES = [
    "global", "fee_recipient", "mint", "bonding_curve", "associated_bonding_curve",
    "associated_user", "user", "system_program", "token_program", "creator_vault",
    "event_authority", "program", "global_volume_accumulator", "user_volume_accumulator",
    "fee_config", "fee_program", "bonding_curve_v2", "buyback_fee_recipient",
]


async def explain(signature: str) -> None:
    settings = load_settings()
    async with RpcClient(settings) as rpc:
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
        print(f"getTransaction returned null for {signature} -- not yet queryable?")
        return

    message = tx["transaction"]["message"]
    account_keys = message["accountKeys"]
    meta = tx["meta"]
    pre_balances = meta["preBalances"]
    post_balances = meta["postBalances"]
    fee = meta["fee"]
    wallet_pubkey = account_keys[0]
    wallet_delta = post_balances[0] - pre_balances[0]

    program_id_str = str(PUMP_FUN_PROGRAM_ID)
    buy_ix = None
    for ix in message["instructions"]:
        if account_keys[ix["programIdIndex"]] != program_id_str:
            continue
        data_bytes = base58.b58decode(ix["data"])
        if data_bytes[:8] == BUY_DISCRIMINATOR:
            buy_ix = ix
            break

    if buy_ix is None:
        print(f"No pump.fun buy instruction found in {signature} -- wrong signature?")
        return

    buy_account_indices = buy_ix["accounts"]
    if len(buy_account_indices) != len(BUY_ACCOUNT_NAMES):
        print(
            f"WARNING: buy instruction has {len(buy_account_indices)} accounts, "
            f"expected {len(BUY_ACCOUNT_NAMES)} per program.py's verified ordering "
            "-- account map may be stale for this transaction."
        )

    print(f"signature: {signature}")
    print(f"wallet (accountKeys[0]): {wallet_pubkey}")
    print(f"base fee: {fee} lamports")
    print(f"wallet balance delta: {wallet_delta} lamports")
    print()
    print(f"{'name':<26} {'pubkey':<46} {'pre':>14} {'post':>14} {'delta':>14}  flag")
    print("-" * 122)

    created_total = 0
    payment_total = 0
    user_volume_accumulator_created = False
    associated_user_created = False

    for name, idx in zip(BUY_ACCOUNT_NAMES, buy_account_indices, strict=False):
        pubkey = account_keys[idx]
        pre = pre_balances[idx]
        post = post_balances[idx]
        delta = post - pre
        created = pre == 0 and post > 0
        flag = ""
        if idx == 0:
            flag = "(wallet/fee payer)"
        elif created:
            flag = "CREATED (rent-funded)"
            created_total += delta
            if name == "user_volume_accumulator":
                user_volume_accumulator_created = True
                # Cross-check: the created account really is this wallet's
                # user_volume_accumulator PDA, not a coincidence of position.
                expected = str(derive_user_volume_accumulator_pda(Pubkey.from_string(wallet_pubkey)))
                if pubkey != expected:
                    flag += f" -- MISMATCH vs derived PDA {expected}"
            if name == "associated_user":
                associated_user_created = True
        elif delta > 0 and idx != 0:
            flag = "payment received"
            payment_total += delta

        print(f"{name:<26} {pubkey:<46} {pre:>14} {post:>14} {delta:>14}  {flag}")

    reconstructed_outflow = fee + payment_total + created_total
    print()
    print(f"base fee:                     {fee:>12} lamports")
    print(f"trade payment (non-created):  {payment_total:>12} lamports")
    print(f"account creation (rent):      {created_total:>12} lamports")
    print(f"reconstructed total outflow:  {reconstructed_outflow:>12} lamports")
    print(f"observed wallet delta:        {wallet_delta:>12} lamports (abs={-wallet_delta})")
    matches = reconstructed_outflow == -wallet_delta
    print(f"reconstruction matches observed delta EXACTLY: {matches}")
    if not matches:
        print(
            f"  DISCREPANCY: {reconstructed_outflow - (-wallet_delta)} lamports unaccounted for -- "
            "investigate before trusting this decomposition."
        )

    print()
    print("=== One-time-PDA hypothesis (Section 4.2) ===")
    print(f"user_volume_accumulator created in this transaction: {user_volume_accumulator_created}")
    print(f"associated_user (ATA) created in this transaction:   {associated_user_created}")
    if user_volume_accumulator_created:
        print(
            "CONFIRMED: this trade paid a one-time user_volume_accumulator rent cost "
            "that will not recur on subsequent trades from this wallet. The "
            f"{-wallet_delta} lamport headline loss on this trade is NOT representative "
            "of steady-state per-trade economics -- do not size a position from it."
        )
    else:
        print(
            "REFUTED: user_volume_accumulator was already funded before this "
            "transaction (or was not part of this instruction's account list at all). "
            "The residual cost, if any, above position + ATA rent + base fee is a "
            "recurring cost and changes the economics in Section 4 -- report it."
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("signature", help="A confirmed pump.fun buy transaction signature")
    args = parser.parse_args()
    asyncio.run(explain(args.signature))


if __name__ == "__main__":
    main()
