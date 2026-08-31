"""pump.fun bonding-curve math: expected output, price impact, completion.

Pure constant-product AMM math against a BondingCurve account's *virtual*
reserves. This module does no RPC calls -- callers fetch the live account
(getAccountInfo on derive_bonding_curve_pda's PDA, see program.py) and decode
it with decode_bonding_curve below before calling anything else here.

FEE_BPS and MIGRATION_SOL_LAMPORTS are pump.fun protocol constants that have
changed before (notably the creator-fee rollout) and can change again --
verify both against a live transaction/curve before trusting this module for
a real trade, same caution as program.py's account ordering.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

FEE_BPS = 100  # 1.00% trading fee, taken off the SOL side. VERIFY before trusting.
BPS_DENOMINATOR = 10_000

# Curve migrates to PumpSwap once real SOL reserves cross this. VERIFY before trusting.
MIGRATION_SOL_LAMPORTS = 85_000_000_000  # 85 SOL

# Anchor account discriminator (8 bytes) + 5x little-endian u64 + 1 bool byte.
# Newer curves may have additional trailing fields (e.g. a creator pubkey);
# those are ignored here since they don't affect this module's math.
_HEADER_STRUCT = struct.Struct("<8s5QB")


@dataclass(frozen=True)
class BondingCurveState:
    virtual_token_reserves: int
    virtual_sol_reserves: int
    real_token_reserves: int
    real_sol_reserves: int
    token_total_supply: int
    complete: bool


class CurveCompleteError(RuntimeError):
    """The curve has migrated to PumpSwap; pump.fun's program rejects trades
    against it. Raised here to fail fast before spending a priority fee."""


def decode_bonding_curve(raw: bytes) -> BondingCurveState:
    """Decode a BondingCurve account's raw bytes.

    NOT verified against a live IDL (scripts/probe.py couldn't find the IDL
    account at its expected PDA) -- cross-check field values against an
    explorer for a known mint before trusting this for a real trade.
    """
    if len(raw) < _HEADER_STRUCT.size:
        raise ValueError(
            f"bonding curve account too short: {len(raw)} bytes, "
            f"need at least {_HEADER_STRUCT.size}"
        )
    (
        _discriminator,
        virtual_token_reserves,
        virtual_sol_reserves,
        real_token_reserves,
        real_sol_reserves,
        token_total_supply,
        complete_byte,
    ) = _HEADER_STRUCT.unpack_from(raw)
    return BondingCurveState(
        virtual_token_reserves=virtual_token_reserves,
        virtual_sol_reserves=virtual_sol_reserves,
        real_token_reserves=real_token_reserves,
        real_sol_reserves=real_sol_reserves,
        token_total_supply=token_total_supply,
        complete=bool(complete_byte),
    )


def _fee(amount_lamports: int) -> int:
    return amount_lamports * FEE_BPS // BPS_DENOMINATOR


def sol_to_tokens(curve: BondingCurveState, sol_in_lamports: int) -> int:
    """Tokens received for a gross SOL input (fee included), via the
    constant-product invariant virtual_sol * virtual_token = k.
    """
    if curve.complete:
        raise CurveCompleteError("bonding curve has completed / migrated")
    if sol_in_lamports <= 0:
        return 0

    net_sol_in = sol_in_lamports - _fee(sol_in_lamports)
    k = curve.virtual_sol_reserves * curve.virtual_token_reserves
    new_virtual_sol = curve.virtual_sol_reserves + net_sol_in
    new_virtual_token = k // new_virtual_sol
    tokens_out = curve.virtual_token_reserves - new_virtual_token
    return max(0, min(tokens_out, curve.real_token_reserves))


def tokens_to_sol(curve: BondingCurveState, tokens_in: int) -> int:
    """Net SOL received for selling tokens_in (fee already deducted)."""
    if curve.complete:
        raise CurveCompleteError("bonding curve has completed / migrated")
    if tokens_in <= 0:
        return 0

    k = curve.virtual_sol_reserves * curve.virtual_token_reserves
    new_virtual_token = curve.virtual_token_reserves + tokens_in
    new_virtual_sol = k // new_virtual_token
    gross_sol_out = curve.virtual_sol_reserves - new_virtual_sol
    return max(0, gross_sol_out - _fee(gross_sol_out))


def spot_price_sol_per_token(curve: BondingCurveState) -> float:
    """Instantaneous marginal price, pre-fee, in SOL per raw token unit."""
    if curve.virtual_token_reserves == 0:
        return 0.0
    return curve.virtual_sol_reserves / curve.virtual_token_reserves


def price_impact_fraction(curve: BondingCurveState, sol_in_lamports: int) -> float:
    """Slippage vs spot: (avg_execution_price - spot_price) / spot_price."""
    tokens_out = sol_to_tokens(curve, sol_in_lamports)
    spot = spot_price_sol_per_token(curve)
    if tokens_out == 0 or spot == 0:
        return 1.0
    avg_price = sol_in_lamports / tokens_out
    return (avg_price - spot) / spot


def curve_completion_fraction(curve: BondingCurveState) -> float:
    """How far toward migration this curve is, by real SOL raised so far.

    Used against config.yaml's filters.tier1.curve_completion_guard_fraction
    to avoid buying into a curve with little room left before migration.
    """
    if MIGRATION_SOL_LAMPORTS == 0:
        return 0.0
    return min(1.0, curve.real_sol_reserves / MIGRATION_SOL_LAMPORTS)
