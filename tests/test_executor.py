import pytest
from solders.pubkey import Pubkey

from pumpbot.executor import (
    UnknownTokenProgramError,
    build_buy_instruction,
    build_sell_instruction,
    resolve_token_program_id,
)
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

# Real, live-captured sell transaction (signature
# 2AUXR6omwFGhctwswhJBXzppeUernTUbggPLMajCUt14R4s8MtXkZRDe7KWYwXn1Dovbuk2WSZfPP92dBxsNoEte),
# one of 13 live sells sampled to independently verify sell's account
# ordering -- it is NOT symmetric with buy (see program.py's module
# docstring): creator_vault/token_program are swapped vs buy, and the two
# volume-accumulator accounts present in buy don't exist in sell at all.
SELL_MINT = "9u3PEdJUSk5rsuWCiqYzbQBqfxnmnXru3bJGbUVLpump"
SELL_USER = "BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s"  # account [6]
SELL_CREATOR = "A3y4JpfHTifnZM8Fiev41kfh4j8jxe7NoZCJY3ryVUmd"  # from bonding curve account data
SELL_FEE_RECIPIENT = "GesfTA3X2arioaHp8bbKdjG9vJtskViWACZoYvxp4twS"  # account [1]
SELL_BUYBACK_FEE_RECIPIENT = "5cjcW9wExnJJiqgLjq7DEG75Pm6JBgE1hNv4B2vHXUW6"  # account [15]
SELL_UNRESOLVED = "C6kyKexUTrxvje3rPtRkf4X6XgTVaa2xXEz2cdm6UxWt"  # account [14], passed through as-is
SELL_AMOUNT = 1506119114329
SELL_MIN_SOL_OUTPUT = 34582381
SELL_DATA_HEX = "33e685a4017f83ad59deb1ab5e0100006daf0f0200000000"

SELL_ACCOUNTS = [
    "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf",
    SELL_FEE_RECIPIENT,
    SELL_MINT,
    "9Gxs76f7UFLWNbUXc1YXCikZgcYXjqQ13Mi1dqqxb9p3",
    "3k5Lz9FCdaA1VH8Y496133CLPS9GFsEH5tCRp2CmmAHa",
    "7dwTwvkH7ENi4YmzuwVDqJAbpAQHq2eBp3EPuVRA6xp9",
    SELL_USER,
    "11111111111111111111111111111111",
    "HMmDpGP2CPGKG3QzAUW4m1FzdUYtMMyC1ebzVMKqPBYF",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
    "8Wf5TiAheLUqBrKXeYg2JtAFFMWtKdG2BSFgqUcPVwTt",
    "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ",
    SELL_UNRESOLVED,
    SELL_BUYBACK_FEE_RECIPIENT,
]

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
        unresolved_account=Pubkey.from_string(REAL_UNRESOLVED_16),
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


def build_real_sample_sell_instruction():
    return build_sell_instruction(
        mint=Pubkey.from_string(SELL_MINT),
        user=Pubkey.from_string(SELL_USER),
        creator=Pubkey.from_string(SELL_CREATOR),
        token_program_id=TOKEN_2022_PROGRAM_ID,
        fee_recipient=Pubkey.from_string(SELL_FEE_RECIPIENT),
        buyback_fee_recipient=Pubkey.from_string(SELL_BUYBACK_FEE_RECIPIENT),
        unresolved_account=Pubkey.from_string(SELL_UNRESOLVED),
        amount_tokens_raw=SELL_AMOUNT,
        min_sol_output_lamports=SELL_MIN_SOL_OUTPUT,
    )


def test_reconstructed_sell_matches_real_transaction_account_by_account():
    ix = build_real_sample_sell_instruction()
    assert str(ix.program_id) == str(PUMP_FUN_PROGRAM_ID)
    reconstructed = [str(am.pubkey) for am in ix.accounts]
    assert reconstructed == SELL_ACCOUNTS


def test_reconstructed_sell_data_matches_real_transaction_bytes():
    ix = build_real_sample_sell_instruction()
    assert bytes(ix.data).hex() == SELL_DATA_HEX


def test_sell_instruction_has_16_accounts_not_18():
    # sell has no volume-accumulator accounts at all -- confirmed against
    # 13 live samples, not assumed. See program.py's module docstring.
    ix = build_real_sample_sell_instruction()
    assert len(ix.accounts) == 16


def test_sell_creator_vault_and_token_program_are_swapped_vs_buy():
    # The one confirmed structural difference besides account count: buy has
    # token_program at [8] and creator_vault at [9]; sell has them the other
    # way around. Assert this explicitly so a future "fix" that makes sell
    # symmetric with buy (the wrong, previously-shipped assumption) fails
    # loudly instead of silently reintroducing the bug.
    ix = build_real_sample_sell_instruction()
    assert str(ix.accounts[8].pubkey) == "HMmDpGP2CPGKG3QzAUW4m1FzdUYtMMyC1ebzVMKqPBYF"
    assert str(ix.accounts[9].pubkey) == "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


def test_sell_data_uses_sell_discriminator_not_buy():
    from pumpbot.program import BUY_DISCRIMINATOR, SELL_DISCRIMINATOR

    ix = build_real_sample_sell_instruction()
    data = bytes(ix.data)
    assert data[:8] == SELL_DISCRIMINATOR
    assert data[:8] != BUY_DISCRIMINATOR


class _FakeRpc:
    """Returns None (account not found) for the first `misses` calls, then a
    real-shaped getAccountInfo response -- simulates the propagation lag a
    brand-new mint exhibits on a live RPC node, without a real network call.
    """

    def __init__(self, misses: int, owner: str = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"):
        self.misses = misses
        self.owner = owner
        self.calls = 0

    async def call(self, method, params):
        self.calls += 1
        if self.calls <= self.misses:
            return None
        return {"value": {"owner": self.owner}}


@pytest.mark.asyncio
async def test_resolve_token_program_id_retries_through_transient_misses():
    rpc = _FakeRpc(misses=2)
    owner = await resolve_token_program_id(
        rpc, Pubkey.from_string(REAL_MINT), retries=3, retry_delay_seconds=0
    )
    assert str(owner) == str(TOKEN_2022_PROGRAM_ID)
    assert rpc.calls == 3


@pytest.mark.asyncio
async def test_resolve_token_program_id_raises_after_exhausting_retries():
    rpc = _FakeRpc(misses=5)
    with pytest.raises(UnknownTokenProgramError):
        await resolve_token_program_id(
            rpc, Pubkey.from_string(REAL_MINT), retries=3, retry_delay_seconds=0
        )
    assert rpc.calls == 3


@pytest.mark.asyncio
async def test_resolve_token_program_id_succeeds_immediately_with_no_misses():
    rpc = _FakeRpc(misses=0)
    owner = await resolve_token_program_id(rpc, Pubkey.from_string(REAL_MINT))
    assert str(owner) == str(TOKEN_2022_PROGRAM_ID)
    assert rpc.calls == 1
