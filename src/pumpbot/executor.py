"""Builds pump.fun buy/sell instructions from the account ordering verified
in program.py's module docstring.

Scope: this module builds `solders.instruction.Instruction` objects only --
no signing, blockhash fetching, or sending. Wiring those into a full
transaction (fee payer, recent blockhash, signing, submission, confirmation
polling per config.yaml's execution.* settings) is a separate, larger piece
of work not covered here.

Status: 17 of 18 buy accounts are confirmed, and sell has its own
independently-confirmed 16-account layout (NOT symmetric with buy -- see
program.py's module docstring). Both layouts have one unresolved account
each (buy's [16], sell's [14] -- same kind of gap, different position and
value). Both build functions require it as an explicit `unresolved_account`
parameter with no default, specifically so nothing can call them without
consciously deciding what to do about it. DO NOT send a live transaction
built from either function until that gap is resolved.

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


async def resolve_token_program_id(rpc: RpcClient, mint: Pubkey) -> Pubkey:
    """Every sampled pump.fun mint used Token-2022, not legacy SPL Token --
    see program.py's module docstring. Never assume; always check."""
    info = await rpc.call("getAccountInfo", [str(mint), {"encoding": "base64"}])
    if info is None or info.get("value") is None:
        raise UnknownTokenProgramError(f"mint account not found: {mint}")
    owner = Pubkey.from_string(info["value"]["owner"])
    if owner not in (TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID):
        raise UnknownTokenProgramError(f"mint {mint} owned by unexpected program {owner}")
    return owner


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

    unresolved_account is exactly what it says: account [16] in
    program.py's verified buy map is NOT resolved (see that module's
    docstring) -- no PDA/ATA hypothesis tried reproduces the on-chain
    value, which doesn't even exist yet in any sample seen. This parameter
    has no default and no derivation on purpose, so nobody can call this
    function without consciously supplying something for it. DO NOT send a
    real transaction built from this function until it's resolved.
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

    fee_recipient/buyback_fee_recipient and unresolved_account carry the
    same meaning as in build_buy_instruction -- see that docstring. Note
    the *values* are not shared between a buy and sell of the same
    trade; each must be resolved/supplied independently.
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
