from solders.pubkey import Pubkey

from pumpbot.executor import build_buy_instruction, build_sell_instruction
from pumpbot.program import PUMP_FUN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID

# Real, live-captured buy transaction (signature
# qZLmYTZrJh1cdpLS8hiYWfwMoTG1jMsAZDRRHzstWjbEZbcfyW22v4JokxGPpUFWFyMfv9rkehaApubkwv6neiM),
# confirmed via Anchor discriminator match. Used to byte-for-byte
# reconstruct the instruction and check it against what actually executed
# on-chain -- the strongest test available short of sending a real trade.
REAL_MINT = "Co1vGQoFLBWuxNNNE5JomAnPfR2YyCD8GRUATVLNpump"
REAL_USER = "ATQfqM1KjEv3hMhRX3NUaCa5V9sYgC2gk6YJKNr4R3uq"  # account [6]
REAL_CREATOR = "3wxDfDhShrp6gG8ptXu5ZrJ5rBbw25AkDijQZWpmbTWY"
REAL_FEE_RECIPIENT = "62qc2CNXwrYqQScmEdiZFFAnJR262PxWEuNQtxfafNgV"  # account [1]
REAL_BUYBACK_FEE_RECIPIENT = "5eHhjP8JaYkz83CWwvGU2uMUXefd3AazWGx4gpcuEEYD"  # account [17]
REAL_UNRESOLVED_16 = "3mVgg1fJstV9utmVqPRVbn4f412HfUt5k4vSsKefYanh"  # account [16], passed through as-is
REAL_AMOUNT = 791311616000000
REAL_MAX_SOL_COST = 2000000000
REAL_DATA_HEX = "66063d1201daebea0040c797b1cf02000094357700000000"

REAL_ACCOUNTS = [
    "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf",
    REAL_FEE_RECIPIENT,
    REAL_MINT,
    "6mownbrneP9fTJZKphLWS5Nfzpo74R3mpSdMm9SxScQp",
    "77hPS5593Zu8id1yYniRJR4awuC5ikzsiC2v1PBL3oSU",
    "2Jxaxsbx77nRthAaX8j68jjsFgM3aBQNQtCpWThCxB6i",
    REAL_USER,
    "11111111111111111111111111111111",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    "CT6ysG4BBGEi6NiMM2txSs9gvFtdLn4kRmJWY7Zke81W",
    "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
    "Hq2wp8uJ9jCPsYgNHex8RtqdvMPfVGoYwjvF1ATiwn2Y",
    "9Ri9ysqtBKVHzeFj3QqqothoWEZyPErj4EEAHm3WRXUa",
    "8Wf5TiAheLUqBrKXeYg2JtAFFMWtKdG2BSFgqUcPVwTt",
    "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ",
    REAL_UNRESOLVED_16,
    REAL_BUYBACK_FEE_RECIPIENT,
]


def build_real_sample_instruction():
    return build_buy_instruction(
        mint=Pubkey.from_string(REAL_MINT),
        user=Pubkey.from_string(REAL_USER),
        creator=Pubkey.from_string(REAL_CREATOR),
        token_program_id=TOKEN_2022_PROGRAM_ID,
        fee_recipient=Pubkey.from_string(REAL_FEE_RECIPIENT),
        buyback_fee_recipient=Pubkey.from_string(REAL_BUYBACK_FEE_RECIPIENT),
        unresolved_account_16=Pubkey.from_string(REAL_UNRESOLVED_16),
        amount_tokens_raw=REAL_AMOUNT,
        max_sol_cost_lamports=REAL_MAX_SOL_COST,
    )


def test_reconstructed_buy_matches_real_transaction_account_by_account():
    ix = build_real_sample_instruction()
    assert str(ix.program_id) == str(PUMP_FUN_PROGRAM_ID)
    reconstructed = [str(am.pubkey) for am in ix.accounts]
    assert reconstructed == REAL_ACCOUNTS


def test_reconstructed_buy_data_matches_real_transaction_bytes():
    ix = build_real_sample_instruction()
    assert bytes(ix.data).hex() == REAL_DATA_HEX


def test_buy_instruction_has_18_accounts():
    ix = build_real_sample_instruction()
    assert len(ix.accounts) == 18


def test_buy_user_is_signer():
    ix = build_real_sample_instruction()
    user_meta = ix.accounts[6]
    assert str(user_meta.pubkey) == REAL_USER
    assert user_meta.is_signer is True


def test_buy_fee_recipient_and_buyback_are_distinct_positions():
    ix = build_real_sample_instruction()
    assert str(ix.accounts[1].pubkey) != str(ix.accounts[17].pubkey)
    assert str(ix.accounts[1].pubkey) == REAL_FEE_RECIPIENT
    assert str(ix.accounts[17].pubkey) == REAL_BUYBACK_FEE_RECIPIENT


def test_sell_instruction_has_same_shape_as_buy():
    sell_ix = build_sell_instruction(
        mint=Pubkey.from_string(REAL_MINT),
        user=Pubkey.from_string(REAL_USER),
        creator=Pubkey.from_string(REAL_CREATOR),
        token_program_id=TOKEN_2022_PROGRAM_ID,
        fee_recipient=Pubkey.from_string(REAL_FEE_RECIPIENT),
        buyback_fee_recipient=Pubkey.from_string(REAL_BUYBACK_FEE_RECIPIENT),
        unresolved_account_16=Pubkey.from_string(REAL_UNRESOLVED_16),
        amount_tokens_raw=1000,
        min_sol_output_lamports=1,
    )
    assert len(sell_ix.accounts) == 18
    # Same account ordering assumption as buy (unverified for sell -- see
    # program.py) but the accounts we CAN check should line up identically.
    buy_ix = build_real_sample_instruction()
    buy_pubkeys = [str(am.pubkey) for am in buy_ix.accounts]
    sell_pubkeys = [str(am.pubkey) for am in sell_ix.accounts]
    assert buy_pubkeys == sell_pubkeys


def test_sell_data_uses_sell_discriminator_not_buy():
    from pumpbot.program import BUY_DISCRIMINATOR, SELL_DISCRIMINATOR

    sell_ix = build_sell_instruction(
        mint=Pubkey.from_string(REAL_MINT),
        user=Pubkey.from_string(REAL_USER),
        creator=Pubkey.from_string(REAL_CREATOR),
        token_program_id=TOKEN_2022_PROGRAM_ID,
        fee_recipient=Pubkey.from_string(REAL_FEE_RECIPIENT),
        buyback_fee_recipient=Pubkey.from_string(REAL_BUYBACK_FEE_RECIPIENT),
        unresolved_account_16=Pubkey.from_string(REAL_UNRESOLVED_16),
        amount_tokens_raw=1000,
        min_sol_output_lamports=1,
    )
    data = bytes(sell_ix.data)
    assert data[:8] == SELL_DISCRIMINATOR
    assert data[:8] != BUY_DISCRIMINATOR
