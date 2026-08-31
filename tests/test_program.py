from solders.pubkey import Pubkey

from pumpbot.program import (
    ASSOCIATED_TOKEN_PROGRAM_ID,
    BUY_DISCRIMINATOR,
    BUYBACK_FEE_RECIPIENTS,
    FEE_CONFIG,
    FEE_PROGRAM_ID,
    NORMAL_FEE_RECIPIENTS,
    PUMP_FUN_PROGRAM_ID,
    RESERVED_FEE_RECIPIENTS,
    SELL_DISCRIMINATOR,
    TOKEN_2022_PROGRAM_ID,
    TOKEN_PROGRAM_ID,
    derive_associated_token_address,
    derive_bonding_curve_pda,
    derive_creator_vault_pda,
    derive_event_authority_pda,
    derive_global_pda,
    derive_global_volume_accumulator_pda,
    derive_user_volume_accumulator_pda,
    pick_buyback_fee_recipient,
    pick_fee_recipient,
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


# The following four match real on-chain values from 3+ distinct live buy
# transactions (see program.py's module docstring for the full account map
# and how each was checked) -- not just internally self-consistent.


def test_derive_creator_vault_pda_matches_confirmed_live_values():
    cases = {
        "CG44pjEEhAirnKrD2Ajp7gzLaMQ2QHyqZAxtPKY4FXAk": "F6FoWChTFCZ2TVHWtMEVATH5GppmJVLDypGvdyYCByQb",
        "3wxDfDhShrp6gG8ptXu5ZrJ5rBbw25AkDijQZWpmbTWY": "CT6ysG4BBGEi6NiMM2txSs9gvFtdLn4kRmJWY7Zke81W",
        "8wMiWbCHi3BqcqHAVzSgGTQrW1EWmNgBfzrAgBo1bUGA": "4tzF5g5ZNbh3TR7GvhsBiqULfx75eyY1WrJsCjUUvuXW",
    }
    for creator, expected_vault in cases.items():
        vault = derive_creator_vault_pda(Pubkey.from_string(creator))
        assert str(vault) == expected_vault


def test_derive_event_authority_pda_matches_confirmed_live_value():
    assert str(derive_event_authority_pda()) == "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1"


def test_derive_global_volume_accumulator_pda_matches_confirmed_live_value():
    assert (
        str(derive_global_volume_accumulator_pda())
        == "Hq2wp8uJ9jCPsYgNHex8RtqdvMPfVGoYwjvF1ATiwn2Y"
    )


def test_derive_user_volume_accumulator_pda_matches_confirmed_live_values():
    cases = {
        "BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s": "FGFrX2q1iAjyAojjeyFDxXqdmvegjPpSWsrPmrJjeQ2f",
        "ATQfqM1KjEv3hMhRX3NUaCa5V9sYgC2gk6YJKNr4R3uq": "9Ri9ysqtBKVHzeFj3QqqothoWEZyPErj4EEAHm3WRXUa",
    }
    for user, expected_uva in cases.items():
        uva = derive_user_volume_accumulator_pda(Pubkey.from_string(user))
        assert str(uva) == expected_uva


def test_fee_program_and_fee_config_constants_are_distinct():
    assert FEE_PROGRAM_ID != FEE_CONFIG


def test_pick_fee_recipient_draws_from_normal_or_reserved_pools():
    combined = set(NORMAL_FEE_RECIPIENTS) | set(RESERVED_FEE_RECIPIENTS)
    for _ in range(20):
        assert str(pick_fee_recipient()) in combined


def test_pick_buyback_fee_recipient_draws_from_buyback_pool_only():
    combined = set(NORMAL_FEE_RECIPIENTS) | set(RESERVED_FEE_RECIPIENTS)
    for _ in range(20):
        picked = str(pick_buyback_fee_recipient())
        assert picked in set(BUYBACK_FEE_RECIPIENTS)
        assert picked not in combined
