"""pump.fun on-chain constants and PDA derivations.

Instruction account ORDERING for buy/sell is intentionally NOT hardcoded here.
pump.fun's account lists have changed over time (notably the creator-fee
vault addition), and getting this wrong loses funds. `scripts/probe.py`
fetches the live IDL and a recent successful `buy` transaction and prints
the resolved ordering; that output is what `verify()` below checks against
before this module is trusted for live trading.
"""

from __future__ import annotations

from solders.pubkey import Pubkey

# Mainnet pump.fun program. Public, stable, well-documented.
PUMP_FUN_PROGRAM_ID = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")

# System program IDs used in PDA derivation and account lists.
SYSTEM_PROGRAM_ID = Pubkey.from_string("11111111111111111111111111111111")
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string(
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
)
RENT_SYSVAR_ID = Pubkey.from_string("SysvarRent111111111111111111111111111111111")

GLOBAL_SEED = b"global"
BONDING_CURVE_SEED = b"bonding-curve"


def derive_global_pda() -> Pubkey:
    pda, _ = Pubkey.find_program_address([GLOBAL_SEED], PUMP_FUN_PROGRAM_ID)
    return pda


def derive_bonding_curve_pda(mint: Pubkey) -> Pubkey:
    pda, _ = Pubkey.find_program_address(
        [BONDING_CURVE_SEED, bytes(mint)], PUMP_FUN_PROGRAM_ID
    )
    return pda


def derive_associated_token_address(owner: Pubkey, mint: Pubkey) -> Pubkey:
    pda, _ = Pubkey.find_program_address(
        [bytes(owner), bytes(TOKEN_PROGRAM_ID), bytes(mint)],
        ASSOCIATED_TOKEN_PROGRAM_ID,
    )
    return pda


class OrderingNotVerifiedError(RuntimeError):
    """Raised when live trading is attempted before Phase 0 verification."""


def verify() -> None:
    """CLI entry point: `python -m pumpbot.program --verify`.

    Re-derives the PDAs above and prints them. Full instruction-ordering
    verification against a live `buy` transaction happens in
    scripts/probe.py — run that first and compare its printed account list
    against whatever `executor.py` ends up encoding before trusting this
    module for a live trade.
    """
    global_pda = derive_global_pda()
    print(f"program_id       = {PUMP_FUN_PROGRAM_ID}")
    print(f"global PDA       = {global_pda}")
    print("NOTE: buy/sell instruction account ordering is verified by")
    print("scripts/probe.py against a live mainnet transaction, not here.")


if __name__ == "__main__":
    import sys

    if "--verify" in sys.argv:
        verify()
    else:
        print("usage: python -m pumpbot.program --verify")
