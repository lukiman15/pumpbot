"""Builds pump.fun buy/sell instructions from the account ordering verified
in program.py's module docstring.

Scope: this module builds `solders.instruction.Instruction` objects only --
no signing, blockhash fetching, or sending. Wiring those into a full
transaction (fee payer, recent blockhash, signing, submission, confirmation
polling per config.yaml's execution.* settings) is a separate, larger piece
of work not covered here.

Status: all 18 of buy's accounts are confirmed, including [16] -- resolved
as bonding_curve_v2 (see program.py's module docstring). Sell has its own
independently-confirmed 16-account layout (NOT symmetric with buy). Sell's
[14] is the one remaining gap: its identity is still unknown (no PDA/ATA
hypothesis reproduces it), but it's confirmed UNVALIDATED by live
simulateTransaction against brand-new mints (two independent samples, a
random pubkey both times) -- the on-chain program never checks it, unlike
buy's [16] which does raise InvalidBondingCurveV2 when wrong. Both build
functions still require their unresolved_account as an explicit parameter
with no default -- not because an arbitrary value is risky for sell (it's
confirmed not to be) but because a future program upgrade could start
validating it, and a silent default would hide that when it happens.
simulateTransaction is not the same as an actual send, though -- run every
transaction through simulateTransaction immediately before sending until
an actual live send has removed all doubt.

is_signer/is_writable flags below are inferred from what each account
plausibly does during a buy (lamports/token balances change -> writable),
not independently confirmed the way the account ORDERING was -- getting an
ordering position wrong silently sends funds to/through the wrong account,
but getting a writable/signer flag wrong fails the transaction at
simulation ("account not writable" / "missing signature"), it does not
silently misdirect money. Still: run everything through simulateTransaction
before ever sending for real.
"""

from __future__ import annotations

import asyncio
import struct

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey

from pumpbot.program import (
    BUY_DISCRIMINATOR,
    FEE_CONFIG,
    FEE_PROGRAM_ID,
    PUMP_FUN_PROGRAM_ID,
    SELL_DISCRIMINATOR,
    SYSTEM_PROGRAM_ID,
    TOKEN_2022_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
    derive_associated_token_address,
    derive_bonding_curve_pda,
    derive_creator_vault_pda,
    derive_event_authority_pda,
    derive_global_pda,
    derive_global_volume_accumulator_pda,
    derive_user_volume_accumulator_pda,
)
from pumpbot.rpc import RpcClient


class UnknownTokenProgramError(RuntimeError):
    """Raised when a mint's owner is neither the legacy SPL Token program
    nor Token-2022 -- an account list built against either assumption
    would be wrong, so refuse rather than guess."""


class MintNotYetVisibleError(UnknownTokenProgramError):
    """Raised when the mint account still isn't visible after retrying --
    distinct from UnknownTokenProgramError's other case (a resolved but
    unexpected owner) because this one is an expected, recoverable
    condition for a brand-new mint, not evidence of a real bug. Callers
    should treat this differently from other failures (see main.py's
    failsafe handling) -- it isn't the kind of failure the failsafe was
    built to detect.
    """


async def resolve_token_program_id(
    rpc: RpcClient, mint: Pubkey, retries: int = 3, retry_delay_seconds: float = 1.0
) -> Pubkey:
    """Every sampled pump.fun mint used Token-2022, not legacy SPL Token --
    see program.py's module docstring. Never assume; always check.

    What looked like a ~10s RPC propagation lag on a brand-new mint (both
    on QuickNode and on a second provider tested side-by-side, ruling out
    "bad node") turned out to be this call defaulting to `finalized`
    commitment, which genuinely does take that long to wait for -- it isn't
    infrastructure lag at all. `confirmed` commitment (still far short of a
    real send-and-confirm cycle, but enough that a wrong price/curve state
    isn't likely to have moved) was measured available within ~0.1s of
    PumpPortal's own notification, live, repeatedly. The retry loop below is
    kept as a safety margin for the genuinely rare case a mint isn't even
    `confirmed` yet, not as the primary fix.
    """
    last_exc: UnknownTokenProgramError | None = None
    for attempt in range(retries):
        info = await rpc.call(
            "getAccountInfo", [str(mint), {"encoding": "base64", "commitment": "confirmed"}]
        )
        if info is not None and info.get("value") is not None:
            owner = Pubkey.from_string(info["value"]["owner"])
            if owner not in (TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID):
                raise UnknownTokenProgramError(
                    f"mint {mint} owned by unexpected program {owner}"
                )
            return owner
        last_exc = MintNotYetVisibleError(f"mint account not found: {mint}")
        if attempt < retries - 1:
            await asyncio.sleep(retry_delay_seconds)
    assert last_exc is not None
    raise last_exc


def _encode_buy_data(amount_tokens_raw: int, max_sol_cost_lamports: int) -> bytes:
    """8-byte discriminator + u64 amount + u64 max_sol_cost, 24 bytes total.

    A live sample executed successfully with exactly this length -- the
    optional trailing Option<bool> field some samples carried is safe to
    omit entirely. See program.py's module docstring.
    """
    return BUY_DISCRIMINATOR + struct.pack("<QQ", amount_tokens_raw, max_sol_cost_lamports)


def _encode_sell_data(amount_tokens_raw: int, min_sol_output_lamports: int) -> bytes:
    """8-byte discriminator + u64 amount + u64 min_sol_output, 24 bytes total.
    Confirmed against a live sell sample of exactly this length and shape.
    See program.py's module docstring."""
    return SELL_DISCRIMINATOR + struct.pack("<QQ", amount_tokens_raw, min_sol_output_lamports)


def build_buy_instruction(
    *,
    mint: Pubkey,
    user: Pubkey,
    creator: Pubkey,
    token_program_id: Pubkey,
    fee_recipient: Pubkey,
    buyback_fee_recipient: Pubkey,
    unresolved_account: Pubkey,
    amount_tokens_raw: int,
    max_sol_cost_lamports: int,
) -> Instruction:
    """Builds pump.fun's legacy `buy` instruction.

    fee_recipient and buyback_fee_recipient are DISTINCT rotating pools --
    confirmed empirically (they vary independently across live samples,
    even when one repeats). Both must come from pump.fun's published lists
    (FEE_RECIPIENTS.md: "Normal"/"Reserved" for fee_recipient, "Buyback"
    for buyback_fee_recipient) -- an arbitrary address here is not
    confirmed to work and was not what any sampled transaction did.

    unresolved_account is account [16] in program.py's verified buy map --
    its identity is unknown (no PDA/ATA hypothesis reproduces it, see that
    module's docstring), but simulateTransaction confirms the current
    on-chain program doesn't validate it: any pubkey works. This parameter
    still has no default on purpose, so nobody can call this function
    without consciously supplying something for it.
    """
    global_pda = derive_global_pda()
    bonding_curve = derive_bonding_curve_pda(mint)
    associated_bonding_curve = derive_associated_token_address(
        bonding_curve, mint, token_program_id
    )
    associated_user = derive_associated_token_address(user, mint, token_program_id)
    creator_vault = derive_creator_vault_pda(creator)
    event_authority = derive_event_authority_pda()
    global_volume_accumulator = derive_global_volume_accumulator_pda()
    user_volume_accumulator = derive_user_volume_accumulator_pda(user)

    accounts = [
        AccountMeta(global_pda, is_signer=False, is_writable=False),
        AccountMeta(fee_recipient, is_signer=False, is_writable=True),
        AccountMeta(mint, is_signer=False, is_writable=False),
        AccountMeta(bonding_curve, is_signer=False, is_writable=True),
        AccountMeta(associated_bonding_curve, is_signer=False, is_writable=True),
        AccountMeta(associated_user, is_signer=False, is_writable=True),
        AccountMeta(user, is_signer=True, is_writable=True),
        AccountMeta(SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
        AccountMeta(token_program_id, is_signer=False, is_writable=False),
        AccountMeta(creator_vault, is_signer=False, is_writable=True),
        AccountMeta(event_authority, is_signer=False, is_writable=False),
        AccountMeta(PUMP_FUN_PROGRAM_ID, is_signer=False, is_writable=False),
        AccountMeta(global_volume_accumulator, is_signer=False, is_writable=True),
        AccountMeta(user_volume_accumulator, is_signer=False, is_writable=True),
        AccountMeta(FEE_CONFIG, is_signer=False, is_writable=False),
        AccountMeta(FEE_PROGRAM_ID, is_signer=False, is_writable=False),
        AccountMeta(unresolved_account, is_signer=False, is_writable=True),
        AccountMeta(buyback_fee_recipient, is_signer=False, is_writable=True),
    ]

    return Instruction(
        PUMP_FUN_PROGRAM_ID,
        _encode_buy_data(amount_tokens_raw, max_sol_cost_lamports),
        accounts,
    )


def build_sell_instruction(
    *,
    mint: Pubkey,
    user: Pubkey,
    creator: Pubkey,
    token_program_id: Pubkey,
    fee_recipient: Pubkey,
    buyback_fee_recipient: Pubkey,
    unresolved_account: Pubkey,
    amount_tokens_raw: int,
    min_sol_output_lamports: int,
) -> Instruction:
    """Builds pump.fun's `sell` instruction.

    sell's account layout is NOT symmetric with buy's -- an earlier version
    of this function assumed it was, and that was wrong. Confirmed against
    13 live sell transactions (see program.py's module docstring): sell has
    16 accounts, not 18 (no volume-accumulator accounts at all), and
    creator_vault/token_program appear in the OPPOSITE order from buy.

    fee_recipient/buyback_fee_recipient carry the same meaning as in
    build_buy_instruction -- see that docstring. Note the *values* are not
    shared between a buy and sell of the same trade; each must be
    resolved/supplied independently.

    unresolved_account (position [14]) is DIFFERENT from buy's [16] in one
    important way: buy's slot is validated by the on-chain program (wrong
    value raises InvalidBondingCurveV2) and has a known identity
    (bonding_curve_v2). Sell's slot's identity is still unknown -- no PDA/
    ATA hypothesis reproduces it -- but is confirmed UNVALIDATED: two
    independent live simulateTransaction samples against brand-new mints,
    each with a random unrelated pubkey here, ran straight past this slot
    into real business logic with no error referencing it at all. Safe to
    submit any value. See program.py's module docstring for the full
    account map and how this was confirmed.
    """
    global_pda = derive_global_pda()
    bonding_curve = derive_bonding_curve_pda(mint)
    associated_bonding_curve = derive_associated_token_address(
        bonding_curve, mint, token_program_id
    )
    associated_user = derive_associated_token_address(user, mint, token_program_id)
    creator_vault = derive_creator_vault_pda(creator)
    event_authority = derive_event_authority_pda()

    accounts = [
        AccountMeta(global_pda, is_signer=False, is_writable=False),
        AccountMeta(fee_recipient, is_signer=False, is_writable=True),
        AccountMeta(mint, is_signer=False, is_writable=False),
        AccountMeta(bonding_curve, is_signer=False, is_writable=True),
        AccountMeta(associated_bonding_curve, is_signer=False, is_writable=True),
        AccountMeta(associated_user, is_signer=False, is_writable=True),
        AccountMeta(user, is_signer=True, is_writable=True),
        AccountMeta(SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
        AccountMeta(creator_vault, is_signer=False, is_writable=True),
        AccountMeta(token_program_id, is_signer=False, is_writable=False),
        AccountMeta(event_authority, is_signer=False, is_writable=False),
        AccountMeta(PUMP_FUN_PROGRAM_ID, is_signer=False, is_writable=False),
        AccountMeta(FEE_CONFIG, is_signer=False, is_writable=False),
        AccountMeta(FEE_PROGRAM_ID, is_signer=False, is_writable=False),
        AccountMeta(unresolved_account, is_signer=False, is_writable=True),
        AccountMeta(buyback_fee_recipient, is_signer=False, is_writable=True),
    ]

    return Instruction(
        PUMP_FUN_PROGRAM_ID,
        _encode_sell_data(amount_tokens_raw, min_sol_output_lamports),
        accounts,
    )
