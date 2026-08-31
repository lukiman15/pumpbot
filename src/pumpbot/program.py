"""pump.fun on-chain constants, PDA derivations, and the legacy `buy`
instruction's account ordering.

Everything below was cross-checked against 6 live mainnet `buy`
transactions (3 distinct creators, all Token-2022 mints), each independently
confirmed as a genuine `buy` via its Anchor discriminator -- not assumed
from position or account count alone. Method: fetch each tx raw, decode the
instruction data's first 8 bytes, compare against sha256("global:buy")[:8].
For each of the 18 accounts, either match it against a known constant, or
derive it from a hypothesis (a PDA seed, an ATA, a getAccountInfo owner
check) and confirm the derivation reproduces the on-chain value across
every sample -- not just once. 17 of 18 positions are confirmed this way;
position [16] is not (see below) -- an earlier pass wrongly called it a
duplicate of [12] from a single coincidental match, caught and corrected
once more samples disproved it. That mistake is the reason every claim
below says how many samples it was checked against, not just "confirmed."

BUY_ACCOUNTS order (index: name -- how it was confirmed):
  0  global                      -- derive_global_pda(), exact match, all samples
  1  fee_recipient               -- matches pump.fun's published fee-recipient
                                     rotation list (FEE_RECIPIENTS.md); rotates,
                                     does not derive from anything sample-specific
  2  mint                        -- the mint being traded
  3  bonding_curve                -- derive_bonding_curve_pda(mint), exact match
  4  associated_bonding_curve     -- ATA(bonding_curve, mint, token_program)
  5  associated_user              -- ATA(user, mint, token_program)
  6  user                        -- the buyer; NOT always the outer tx's fee
                                     payer -- these buys were CPI'd, and the
                                     calling program can substitute its own
                                     account here. Use account [6] itself as
                                     ground truth, never assume it equals the
                                     transaction signer.
  7  system_program               -- SYSTEM_PROGRAM_ID, exact match
  8  token_program                 -- Token-2022 in every sample (see below) --
                                     NOT safe to hardcode as legacy SPL Token
  9  creator_vault                -- seeds [b"creator-vault", creator], confirmed
                                     for 3 distinct creators
  10 event_authority               -- seeds [b"__event_authority"] (standard
                                     Anchor convention), exact match
  11 program                      -- PUMP_FUN_PROGRAM_ID itself, exact match
  12 global_volume_accumulator    -- seeds [b"global_volume_accumulator"],
                                     exact match; referenced again at [16]
  13 user_volume_accumulator      -- seeds [b"user_volume_accumulator", user],
                                     confirmed for every sample
  14 fee_config                   -- constant across all samples; account is
                                     owned by FEE_PROGRAM_ID (confirmed via
                                     getAccountInfo)
  15 fee_program                  -- constant across all samples; confirmed
                                     executable=true, owned by the BPF
                                     upgradeable loader -- a real program, not
                                     a data PDA
  16 UNRESOLVED -- NOT the same as [12]. An earlier pass wrongly concluded
      this duplicates global_volume_accumulator because they happened to
      coincide in the very first sample decoded; sampling 4 more buys
      disproved that (different value per sample, varying with BOTH user
      and mint -- same user, different mint gives a different [16]). The
      address does not exist on-chain yet in any sample seen (uninitialized
      / lazily created on first use, most likely an ATA-shaped PDA keyed by
      something like the account at [13] -- but no ATA or PDA-seed
      hypothesis tried so far reproduces it, including guesses against
      wrapped SOL and against the mint itself under both token programs).
      Do not populate this position by guessing. buy_v2's published account
      list separately names both `user_volume_accumulator` (confirmed at
      [13] above) and `associated_user_volume_accumulator` -- this is
      almost certainly the legacy equivalent of the latter, but that is an
      inference, not a confirmed derivation.
  17 buyback_fee_recipient       -- CONFIRMED DISTINCT from [1]'s fee_recipient
      (they vary independently across samples, including staying different
      when [1] repeats) -- every observed value matched pump.fun's published
      "Buyback Fee Recipients" list specifically (FEE_RECIPIENTS.md), never
      the "Normal"/"Reserved" lists [1] draws from

Instruction data (buy): 8-byte discriminator + u64 amount (LE, tokens out,
raw units) + u64 max_sol_cost (LE, lamports). A live sample executed
successfully with EXACTLY these 24 bytes and no more -- a trailing
Option<bool> field (probably `track_volume`) is optional and safe to omit
entirely; do not add it.

Token-2022 finding: every sampled mint's owner was Token-2022
(TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb), not legacy SPL Token,
regardless of PumpPortal's `is_mayhem_mode` flag (that correlation was
checked and is false). Resolve token_program per-mint via getAccountInfo on
the mint -- never assume either program.

SELL is NOT symmetric with buy -- this was an assumption in an earlier pass
and it was WRONG. Confirmed by sampling 13 live mainnet `sell` transactions
(discriminator-matched the same way as buy) and cross-checking every
position against the same derivations used for buy:

SELL_ACCOUNTS order (16 accounts, not 18 -- see below):
  0  global                      -- same as buy
  1  fee_recipient               -- same rotating pool as buy's [1] (Normal/
                                     Reserved lists in FEE_RECIPIENTS.md)
  2  mint
  3  bonding_curve                -- same as buy
  4  associated_bonding_curve     -- same as buy
  5  associated_user              -- same as buy, EXCEPT one sample (of 13)
                                     where this position held a value that is
                                     not a valid ATA for the tx's account [6]
                                     under either token program -- unexplained,
                                     see note below. 12 of 13 matched cleanly.
  6  user                        -- same caveat as buy: CPI'd, don't assume
                                     it equals the outer tx's signer
  7  system_program               -- same as buy
  8  creator_vault                -- SWAPPED vs buy: buy has token_program
                                     here. Confirmed via each mint's bonding
                                     curve account, which stores the creator
                                     pubkey at bytes [49:81] (right after the
                                     8-byte disc + 5 u64 + 1 bool = 49 bytes);
                                     derive_creator_vault_pda(that creator)
                                     matched this position in every sample.
  9  token_program                 -- SWAPPED vs buy: buy has creator_vault
                                     here. Token-2022 in every sample checked.
  10 event_authority               -- same as buy
  11 program                      -- same as buy
  12 fee_config                   -- shifted up from buy's [14] because sell
                                     has no volume-accumulator accounts at all
  13 fee_program                  -- shifted up from buy's [15]
  14 UNRESOLVED -- the same unresolved account as buy's [16] (not the same
      VALUE, but the same kind of slot): across samples it's constant for a
      given (user, mint) pair and varies when either changes, matching buy's
      characterization exactly. No PDA/ATA hypothesis tried for buy's [16]
      has been retried against this to actually resolve it -- still opaque,
      still gated behind a required parameter, do not guess.
  15 buyback_fee_recipient       -- confirmed against pump.fun's published
      "Buyback Fee Recipients" list (FEE_RECIPIENTS.md), same role as buy's
      [17]

sell has NO global_volume_accumulator / user_volume_accumulator accounts --
this isn't an oversight, it's a real structural difference from buy (16
accounts total, not 18). Do not add them to a sell instruction.

Instruction data (sell): same 24-byte shape as buy -- 8-byte discriminator +
u64 amount (LE, tokens in, raw units) + u64 min_sol_output (LE, lamports).
Confirmed against a live sample that executed successfully with exactly
this length.

One anomaly: 1 of 13 sampled sells had a position-[5] value that isn't a
valid associated_user ATA for the tx's account [6] under either token
program, despite every other position (including the pump.fun-owned
constants at [10]-[13]) matching cleanly -- so it's genuinely pump.fun's
sell, not a different program's instruction sharing the discriminator by
name collision. Root cause not identified; treated as an open question
rather than folded into the general rule, per this module's standing policy
of not smoothing over an unexplained data point.
"""

from __future__ import annotations

import hashlib

from solders.pubkey import Pubkey

# Mainnet pump.fun program. Public, stable, well-documented.
PUMP_FUN_PROGRAM_ID = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")

# Separate program that owns FEE_CONFIG (account [14]); confirmed executable
# via getAccountInfo, not a guess. Constant across every sample.
FEE_PROGRAM_ID = Pubkey.from_string("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ")
FEE_CONFIG = Pubkey.from_string("8Wf5TiAheLUqBrKXeYg2JtAFFMWtKdG2BSFgqUcPVwTt")

SYSTEM_PROGRAM_ID = Pubkey.from_string("11111111111111111111111111111111")
# Legacy SPL Token program. NOT safe to assume for every mint -- every
# sampled mint used Token-2022 instead. Resolve the real token program
# per-mint (getAccountInfo owner) before using either constant.
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM_ID = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string(
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
)
RENT_SYSVAR_ID = Pubkey.from_string("SysvarRent111111111111111111111111111111111")

GLOBAL_SEED = b"global"
BONDING_CURVE_SEED = b"bonding-curve"
CREATOR_VAULT_SEED = b"creator-vault"
EVENT_AUTHORITY_SEED = b"__event_authority"
GLOBAL_VOLUME_ACCUMULATOR_SEED = b"global_volume_accumulator"
USER_VOLUME_ACCUMULATOR_SEED = b"user_volume_accumulator"


def _anchor_discriminator(instruction_name: str) -> bytes:
    return hashlib.sha256(f"global:{instruction_name}".encode()).digest()[:8]


# CONFIRMED against 6 live buy txs: matched exactly every time.
BUY_DISCRIMINATOR = _anchor_discriminator("buy")
# CONFIRMED against 13 live sell txs: matched exactly every time.
SELL_DISCRIMINATOR = _anchor_discriminator("sell")


def derive_global_pda() -> Pubkey:
    pda, _ = Pubkey.find_program_address([GLOBAL_SEED], PUMP_FUN_PROGRAM_ID)
    return pda


def derive_bonding_curve_pda(mint: Pubkey) -> Pubkey:
    pda, _ = Pubkey.find_program_address(
        [BONDING_CURVE_SEED, bytes(mint)], PUMP_FUN_PROGRAM_ID
    )
    return pda


def derive_creator_vault_pda(creator: Pubkey) -> Pubkey:
    """Confirmed for 3 distinct creators against live buy txs."""
    pda, _ = Pubkey.find_program_address(
        [CREATOR_VAULT_SEED, bytes(creator)], PUMP_FUN_PROGRAM_ID
    )
    return pda


def derive_event_authority_pda() -> Pubkey:
    """Standard Anchor convention; confirmed exact match against live txs."""
    pda, _ = Pubkey.find_program_address([EVENT_AUTHORITY_SEED], PUMP_FUN_PROGRAM_ID)
    return pda


def derive_global_volume_accumulator_pda() -> Pubkey:
    pda, _ = Pubkey.find_program_address(
        [GLOBAL_VOLUME_ACCUMULATOR_SEED], PUMP_FUN_PROGRAM_ID
    )
    return pda


def derive_user_volume_accumulator_pda(user: Pubkey) -> Pubkey:
    """Confirmed for every sampled user against live buy txs."""
    pda, _ = Pubkey.find_program_address(
        [USER_VOLUME_ACCUMULATOR_SEED, bytes(user)], PUMP_FUN_PROGRAM_ID
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

    Re-derives the PDAs above and prints them, plus what's been confirmed
    vs. not (see module docstring for the full account map and how each
    entry was checked).
    """
    print(f"program_id                 = {PUMP_FUN_PROGRAM_ID}")
    print(f"global PDA                 = {derive_global_pda()}")
    print(f"event_authority PDA        = {derive_event_authority_pda()}")
    print(f"global_volume_accum PDA    = {derive_global_volume_accumulator_pda()}")
    print(f"fee_program                = {FEE_PROGRAM_ID}")
    print(f"fee_config                 = {FEE_CONFIG}")
    print(f"buy discriminator          = {BUY_DISCRIMINATOR.hex()} (confirmed live, 6 samples)")
    print(f"sell discriminator         = {SELL_DISCRIMINATOR.hex()} (confirmed live, 13 samples)")
    print()
    print("Confirmed: 17 of buy's 18 accounts (see module docstring). Account")
    print("[16] is NOT resolved -- it doesn't exist on-chain in any sample seen")
    print("and no PDA/ATA hypothesis tried reproduces it. Do not send a live buy")
    print("until it's resolved or explicitly supplied by a caller who has verified it.")
    print("Confirmed: sell has its OWN 16-account layout, NOT symmetric with buy --")
    print("creator_vault/token_program are swapped vs buy, and both volume-")
    print("accumulator accounts are absent entirely. Its own unresolved slot")
    print("(position [14]) is the same kind of gap as buy's [16] -- still unsolved.")


if __name__ == "__main__":
    import sys

    if "--verify" in sys.argv:
        verify()
    else:
        print("usage: python -m pumpbot.program --verify")
