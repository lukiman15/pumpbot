"""pump.fun on-chain constants and PDA derivations.

Instruction account ORDERING for buy/sell is intentionally NOT hardcoded
here -- see VERIFICATION.md-equivalent notes below. What IS captured here
is only what was directly confirmed against a live mainnet `buy`
transaction (signature PWXauggn5HXwguRt61uvRj8CjnErFpbXn61wq8fwWe5jHa2cb7ML26anv3Qjv6MMcBMDJWPZPDuuK1Zd9JjEcye):

  - The instruction's 8-byte Anchor discriminator matched
    sha256("global:buy")[:8] exactly -- confirmed BUY_DISCRIMINATOR below.
  - Account [0] of that instruction equals derive_global_pda() exactly --
    confirms both the PDA derivation and that "global" is account index 0.
  - PUMP_FUN_PROGRAM_ID and SYSTEM_PROGRAM_ID both appeared, at their
    expected values, among that instruction's 18 accounts (positions 11
    and 7 respectively -- not yet confirmed as *stable* positions across
    other mints/transactions, just confirmed present in this one).
  - RENT_SYSVAR_ID did NOT appear anywhere in that instruction's accounts.
  - Critically: the account at the position a "token_program" would occupy
    was Token-2022's program ID (TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb),
    NOT the legacy SPL Token program TOKEN_PROGRAM_ID hardcodes below. Some
    pump.fun mints use Token-2022, not legacy SPL Token -- which token
    program a given mint uses MUST be resolved per-mint (check what program
    owns the mint account) before building any instruction or deriving any
    ATA for it. TOKEN_PROGRAM_ID is kept here as the legacy-SPL default,
    not a safe universal assumption.
  - pump.fun's current public docs (pump-fun/pump-public-docs) only
    document a newer `buy_v2` (27 accounts, quote_mint/fee-sharing/volume-
    accumulator accounts) that this transaction did NOT use -- it used the
    legacy 18-account `buy`, which has no published account-name mapping
    anywhere found. The remaining ~13 unlabeled account positions in that
    instruction are NOT guessed at here; do not hand-write buy/sell
    instruction encoding from position alone. Either find/vet an actively
    maintained pump.fun SDK that already encodes the legacy instruction
    correctly, or sample multiple more live buy transactions (varying
    mints, including Token-2022 ones) to narrow down which positions are
    constant vs. per-mint before writing executor.py.
"""

from __future__ import annotations

import hashlib

from solders.pubkey import Pubkey

# Mainnet pump.fun program. Public, stable, well-documented.
PUMP_FUN_PROGRAM_ID = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")

# System program IDs used in PDA derivation and account lists.
SYSTEM_PROGRAM_ID = Pubkey.from_string("11111111111111111111111111111111")
# Legacy SPL Token program. NOT safe to assume for every mint -- see module
# docstring. Resolve the real token program per-mint before using this.
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM_ID = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string(
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
)
RENT_SYSVAR_ID = Pubkey.from_string("SysvarRent111111111111111111111111111111111")

GLOBAL_SEED = b"global"
BONDING_CURVE_SEED = b"bonding-curve"


def _anchor_discriminator(instruction_name: str) -> bytes:
    return hashlib.sha256(f"global:{instruction_name}".encode()).digest()[:8]


# CONFIRMED against the live tx cited above: matched exactly.
BUY_DISCRIMINATOR = _anchor_discriminator("buy")
# Computed the same way; NOT yet confirmed against a live `sell` tx.
SELL_DISCRIMINATOR = _anchor_discriminator("sell")


def derive_global_pda() -> Pubkey:
    pda, _ = Pubkey.find_program_address([GLOBAL_SEED], PUMP_FUN_PROGRAM_ID)
    return pda


def derive_bonding_curve_pda(mint: Pubkey) -> Pubkey:
    pda, _ = Pubkey.find_program_address(
        [BONDING_CURVE_SEED, bytes(mint)], PUMP_FUN_PROGRAM_ID
    )
    return pda


def derive_associated_token_address(
    owner: Pubkey, mint: Pubkey, token_program_id: Pubkey
) -> Pubkey:
    """token_program_id must be resolved per-mint (legacy SPL Token vs.
    Token-2022) -- it's part of the ATA's PDA seeds, so guessing wrong
    derives the wrong address entirely. See module docstring."""
    pda, _ = Pubkey.find_program_address(
        [bytes(owner), bytes(token_program_id), bytes(mint)],
        ASSOCIATED_TOKEN_PROGRAM_ID,
    )
    return pda


class OrderingNotVerifiedError(RuntimeError):
    """Raised when live trading is attempted before Phase 0 verification."""


def verify() -> None:
    """CLI entry point: `python -m pumpbot.program --verify`.

    Re-derives the PDAs above and prints them, plus what's actually been
    confirmed against a live transaction (see module docstring) vs. what
    hasn't. Full account-ordering verification for the legacy `buy`/`sell`
    instructions remains incomplete -- do not trust this module for a live
    trade until that's resolved (an SDK cross-check or more sampled
    transactions), not just this printout.
    """
    global_pda = derive_global_pda()
    print(f"program_id           = {PUMP_FUN_PROGRAM_ID}")
    print(f"global PDA           = {global_pda}")
    print(f"buy discriminator    = {BUY_DISCRIMINATOR.hex()} (confirmed live)")
    print(f"sell discriminator   = {SELL_DISCRIMINATOR.hex()} (computed, NOT confirmed live)")
    print()
    print("NOT verified: full account ordering for legacy buy/sell (18 accounts,")
    print("no published account-name mapping found -- pump.fun's public docs only")
    print("cover the newer buy_v2). Which token program (legacy SPL vs Token-2022)")
    print("a mint uses must be resolved per-mint, not assumed from TOKEN_PROGRAM_ID.")


if __name__ == "__main__":
    import sys

    if "--verify" in sys.argv:
        verify()
    else:
        print("usage: python -m pumpbot.program --verify")
