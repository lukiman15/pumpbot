import asyncio
import json

import pytest

from pumpbot.config import Tier1FilterConfig
from pumpbot.filters.tier1 import Tier1Filter
from pumpbot.listener import (
    MintListener,
    event_to_candidate,
    parse_create_message,
)

# A real captured PumpPortal create message.
LIVE_CREATE_MESSAGE = {
    "signature": "5Pd57QB7u4ZMupYy7S1ZRNybR8LjX9LLBpLtBWrFZi92NooYBxTw2iCK5yWamnNo55UzB5GHTKhLXMo6GbFNAVew",
    "mint": "EczjCg25qBFUYRACbijFTyB3k1kDbueYRcjnN6oFpump",
    "traderPublicKey": "97K6nsFhBWDKwQf6heDDhDtsCRC4779LPHSFZkc2zqK4",
    "txType": "create",
    "initialBuy": 35323.892708,
    "solAmount": 0.000987653,
    "bondingCurveKey": "9TvfvsrnPRDygLF9zwNoVwoK42KA5ZnnbxumVCMocT1q",
    "vTokensInBondingCurve": 1072964676.107292,
    "vSolInBondingCurve": 30.00098765299999,
    "marketCapSol": 27.960834425456905,
    "name": "solly",
    "symbol": "solly",
    "uri": "https://meta.uxento.io/data/fbfbd14e-5ffb-4e06-9585-a5610d73fb89",
    "is_mayhem_mode": False,
    "pool": "pump",
}

CONFIG = Tier1FilterConfig(
    max_creator_supply_fraction=0.10,
    max_mints_per_second=5,
    curve_completion_guard_fraction=0.80,
)


def test_parse_create_message_extracts_fields():
    event = parse_create_message(LIVE_CREATE_MESSAGE, notified_at=100.0)
    assert event is not None
    assert event.mint == "EczjCg25qBFUYRACbijFTyB3k1kDbueYRcjnN6oFpump"
    assert event.creator == "97K6nsFhBWDKwQf6heDDhDtsCRC4779LPHSFZkc2zqK4"
    assert event.name == "solly"
    assert event.symbol == "solly"
    assert event.initial_buy_whole_tokens == pytest.approx(35323.892708)


def test_parse_create_message_ignores_non_create_txtype():
    msg = {**LIVE_CREATE_MESSAGE, "txType": "buy"}
    assert parse_create_message(msg, notified_at=100.0) is None


def test_parse_create_message_ignores_subscription_ack():
    ack = {"message": "Successfully subscribed to token creation events."}
    assert parse_create_message(ack, notified_at=100.0) is None


def test_parse_create_message_ignores_missing_mint():
    msg = {**LIVE_CREATE_MESSAGE}
    del msg["mint"]
    assert parse_create_message(msg, notified_at=100.0) is None


def test_event_to_candidate_creator_supply_fraction_is_small():
    event = parse_create_message(LIVE_CREATE_MESSAGE, notified_at=100.0)
    candidate = event_to_candidate(event)
    # ~35,324 tokens out of a 1B supply -- tiny, well under the 10% threshold.
    assert candidate.creator_supply_fraction == pytest.approx(0.0000353, abs=1e-6)
    assert candidate.mint == event.mint
    assert not candidate.curve.complete
    assert candidate.is_mayhem_mode is False


@pytest.mark.asyncio
async def test_handle_raw_message_rejects_mayhem_mode():
    # Confirmed root cause of intermittent fee_recipient NotAuthorized
    # failures -- see filters/tier1.py's Candidate docstring.
    queue: asyncio.Queue = asyncio.Queue()
    filt = Tier1Filter(CONFIG, set(), set())
    listener = MintListener(filt, queue)

    listener._handle_raw_message(
        json.dumps({**LIVE_CREATE_MESSAGE, "is_mayhem_mode": True})
    )

    assert queue.qsize() == 0


@pytest.mark.asyncio
async def test_handle_raw_message_accepts_clean_candidate():
    queue: asyncio.Queue = asyncio.Queue()
    filt = Tier1Filter(CONFIG, set(), set())
    listener = MintListener(filt, queue)

    listener._handle_raw_message(json.dumps(LIVE_CREATE_MESSAGE))

    assert queue.qsize() == 1
    candidate = queue.get_nowait()
    assert candidate.mint == LIVE_CREATE_MESSAGE["mint"]


@pytest.mark.asyncio
async def test_handle_raw_message_rejects_blocked_creator():
    queue: asyncio.Queue = asyncio.Queue()
    filt = Tier1Filter(CONFIG, {LIVE_CREATE_MESSAGE["traderPublicKey"]}, set())
    listener = MintListener(filt, queue)

    listener._handle_raw_message(json.dumps(LIVE_CREATE_MESSAGE))

    assert queue.qsize() == 0


@pytest.mark.asyncio
async def test_handle_raw_message_ignores_malformed_json():
    queue: asyncio.Queue = asyncio.Queue()
    filt = Tier1Filter(CONFIG, set(), set())
    listener = MintListener(filt, queue)

    listener._handle_raw_message("{not valid json")

    assert queue.qsize() == 0


@pytest.mark.asyncio
async def test_handle_raw_message_rejects_after_mint_rate_exceeded():
    queue: asyncio.Queue = asyncio.Queue()
    filt = Tier1Filter(CONFIG, set(), set())
    listener = MintListener(filt, queue)

    # max_mints_per_second is 5 -- the 6th create within the window should reject.
    for i in range(6):
        msg = {**LIVE_CREATE_MESSAGE, "mint": f"Mint{i}"}
        listener._handle_raw_message(json.dumps(msg))

    accepted = []
    while not queue.empty():
        accepted.append(queue.get_nowait())
    assert len(accepted) == 5


@pytest.mark.asyncio
async def test_handle_raw_message_drops_when_queue_full():
    queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    filt = Tier1Filter(CONFIG, set(), set())
    listener = MintListener(filt, queue)

    listener._handle_raw_message(json.dumps({**LIVE_CREATE_MESSAGE, "mint": "Mint1"}))
    listener._handle_raw_message(json.dumps({**LIVE_CREATE_MESSAGE, "mint": "Mint2"}))

    assert queue.qsize() == 1
    assert queue.get_nowait().mint == "Mint1"
