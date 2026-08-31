from solders.pubkey import Pubkey

from pumpbot.program import (
    ASSOCIATED_TOKEN_PROGRAM_ID,
    BUY_DISCRIMINATOR,
    PUMP_FUN_PROGRAM_ID,
    SELL_DISCRIMINATOR,
    TOKEN_2022_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
    derive_associated_token_address,
    derive_bonding_curve_pda,
    derive_global_pda,
)


def test_buy_discriminator_matches_confirmed_live_value():
    # Confirmed against a live mainnet buy tx -- see program.py's module docstring.
    assert BUY_DISCRIMINATOR.hex() == "66063d1201daebea"


def test_sell_discriminator_is_deterministic_and_distinct_from_buy():
    assert SELL_DISCRIMINATOR != BUY_DISCRIMINATOR
    assert len(SELL_DISCRIMINATOR) == 8


def test_derive_global_pda_is_deterministic():
    assert derive_global_pda() == derive_global_pda()


def test_derive_global_pda_matches_confirmed_live_value():
    # Account [0] of the live-verified buy tx (see program.py's module
    # docstring) equals this exactly -- confirms both the derivation and
    # that "global" is account index 0 for the legacy buy instruction.
    assert str(derive_global_pda()) == "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf"


def test_derive_bonding_curve_pda_differs_per_mint():
    mint_a = Pubkey.new_unique()
    mint_b = Pubkey.new_unique()
    assert derive_bonding_curve_pda(mint_a) != derive_bonding_curve_pda(mint_b)


def test_derive_associated_token_address_differs_by_token_program():
    owner = Pubkey.new_unique()
    mint = Pubkey.new_unique()
    legacy_ata = derive_associated_token_address(owner, mint, TOKEN_PROGRAM_ID)
    token2022_ata = derive_associated_token_address(owner, mint, TOKEN_2022_PROGRAM_ID)
    # The token program is part of the ATA's PDA seeds -- guessing the wrong
    # one derives a completely different (wrong) address, not a variant of
    # the right one. This is exactly the Token-2022 finding from verification.
    assert legacy_ata != token2022_ata


def test_derive_associated_token_address_is_deterministic():
    owner = Pubkey.new_unique()
    mint = Pubkey.new_unique()
    a = derive_associated_token_address(owner, mint, TOKEN_PROGRAM_ID)
    b = derive_associated_token_address(owner, mint, TOKEN_PROGRAM_ID)
    assert a == b


def test_known_program_ids_are_well_formed():
    # from_string already validates base58/length -- these just confirm the
    # module-level constants import and construct without error.
    assert PUMP_FUN_PROGRAM_ID is not None
    assert ASSOCIATED_TOKEN_PROGRAM_ID is not None
    assert TOKEN_PROGRAM_ID != TOKEN_2022_PROGRAM_ID
