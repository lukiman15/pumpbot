"""Phase 0 gate. Read-only, near-zero cost. Run this BEFORE building anything else.

    python scripts/probe.py --hours 1

Answers three questions (see PLAN.md "Phase 0 -- the gate"):

  1. Buy-queue rank: for each observed pump.fun `create`, how many buys on
     that mint already landed before our websocket notification arrived?
  2. IDL / account ordering: fetch the on-chain IDL and a recent successful
     `buy` transaction, print both so account ordering can be eyeballed
     against what program.py assumes.
  3. Fee-drag reality: sample live prioritization fees and compute what
     fraction of candidates the breakeven guard (config.yaml fees.*) would
     reject, plus how many tier-1 survivors show up per hour.

This script does not place any orders and does not require a funded wallet.
It needs QUICKNODE_HTTP_URL in .env for RPC lookups (fee samples, mint
resolution, IDL/account-ordering checks). New-mint DETECTION uses
PumpPortal's free public websocket (wss://pumpportal.fun/api/data) instead
of QuickNode's own logsSubscribe -- subscribing to "mentions the pump.fun
program" delivers every buy/sell across every token on the platform, not
just creates, and that firehose got the raw WSS approach killed repeatedly
even against QuickNode's own gateway. PumpPortal filters server-side to
just new-token events, which is the volume this is actually built for.
PumpPortal is a third-party service with no SLA -- if it lags or drops,
that's a real input into the buy-queue-rank number, not a bug to paper
over.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import websockets

from pumpbot.config import load_settings
from pumpbot.program import PUMP_FUN_PROGRAM_ID
from pumpbot.rpc import DailyLimitReachedError, RpcClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("probe")

PUMPPORTAL_WS_URL = "wss://pumpportal.fun/api/data"


@dataclass
class CreateObservation:
    signature: str
    mint: str | None
    notified_at: float
    buys_before_us: int | None = None
    priority_fee_lamports: int | None = None


@dataclass
class ProbeResults:
    creates_seen: int = 0
    observations: list[CreateObservation] = field(default_factory=list)
    fee_samples_lamports: list[int] = field(default_factory=list)
    tier1_survivors: int = 0
    fee_drag_rejections: int = 0
    fee_drag_candidates: int = 0


async def listen_for_creates(
    settings, results: ProbeResults, duration_seconds: float, queue: asyncio.Queue
) -> None:
    """Subscribe to PumpPortal's free new-token feed, push observations onto the queue.

    PumpPortal filters server-side to just `create` events, so this carries
    none of the firehose volume that killed QuickNode's raw `logsSubscribe`
    (which delivers every buy/sell on the program, not just creates).
    Reconnects with exponential backoff + jitter, capped at 60s, resetting
    only once a connection survives 30s -- so a single blip doesn't leave
    us stuck on a long delay, but a real problem doesn't get hammered.
    """
    import random

    deadline = time.monotonic() + duration_seconds
    backoff = 2.0
    max_backoff = 60.0

    while time.monotonic() < deadline:
        connected_at = time.monotonic()
        survived_this_connection = False
        try:
            async with websockets.connect(PUMPPORTAL_WS_URL, ping_interval=20) as ws:
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                logger.info("subscribed to PumpPortal new-token feed")

                while time.monotonic() < deadline:
                    timeout = max(0.1, deadline - time.monotonic())
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                    except asyncio.TimeoutError:
                        break
                    notified_at = time.monotonic()
                    if time.monotonic() - connected_at > 30:
                        survived_this_connection = True
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if msg.get("txType") != "create" or not msg.get("mint"):
                        continue
                    results.creates_seen += 1
                    obs = CreateObservation(
                        signature=msg.get("signature", ""),
                        mint=msg["mint"],
                        notified_at=notified_at,
                    )
                    results.observations.append(obs)
                    await queue.put(obs)
                    logger.info(
                        "create #%d mint=%s sig=%s",
                        results.creates_seen, obs.mint, obs.signature,
                    )
        except (websockets.ConnectionClosed, OSError) as exc:
            if survived_this_connection:
                backoff = 2.0
            sleep_for = min(max_backoff, backoff) + random.uniform(0, 1)
            logger.warning(
                "PumpPortal websocket dropped (%s), reconnecting in %.1fs",
                exc, sleep_for,
            )
            await asyncio.sleep(sleep_for)
            backoff = min(max_backoff, backoff * 2)


MIN_TX_AGE_S = 20.0
GET_TX_RETRIES = 4
GET_TX_RETRY_DELAY_S = 5.0


async def resolve_mint_and_rank(rpc: RpcClient, obs: CreateObservation) -> None:
    """Given a mint+signature from PumpPortal, count buys that beat us to it.

    A signature this fresh usually isn't indexed yet -- getTransaction returns
    null for several seconds after the notification arrives. Wait past that
    window and retry a few times before giving up; otherwise this resolves
    nothing no matter how long the probe runs.
    """
    if not obs.signature:
        return

    wait_for = max(0.0, (obs.notified_at + MIN_TX_AGE_S) - time.monotonic())
    if wait_for:
        await asyncio.sleep(wait_for)

    tx = None
    for attempt in range(GET_TX_RETRIES):
        try:
            tx = await rpc.call(
                "getTransaction",
                [obs.signature, {"maxSupportedTransactionVersion": 0, "encoding": "jsonParsed"}],
            )
        except DailyLimitReachedError:
            raise
        except Exception as exc:  # noqa: BLE001 - probe is best-effort, log and move on
            logger.warning("getTransaction failed for %s: %s", obs.signature, exc)
            return
        if tx is not None:
            break
        if attempt < GET_TX_RETRIES - 1:
            await asyncio.sleep(GET_TX_RETRY_DELAY_S)
    if tx is None:
        logger.warning(
            "getTransaction never resolved for %s after %d attempts",
            obs.signature, GET_TX_RETRIES,
        )
        return

    block_time = tx.get("blockTime")
    if block_time is None:
        return

    # Count buy transactions referencing this mint that landed in the
    # ~2 seconds after creation but before our notification's wall-clock
    # arrival would have let us submit (approximated as notified_at itself,
    # since that is the earliest instant our own bot could react).
    try:
        sigs = await rpc.call(
            "getSignaturesForAddress",
            [obs.mint, {"limit": 50}],
        )
    except DailyLimitReachedError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("getSignaturesForAddress failed for %s: %s", obs.mint, exc)
        return

    buys_before = 0
    for entry in sigs:
        if entry["signature"] == obs.signature:
            continue
        entry_block_time = entry.get("blockTime")
        if entry_block_time is not None and entry_block_time <= block_time + 2:
            buys_before += 1
    obs.buys_before_us = buys_before


async def sample_priority_fees(rpc: RpcClient, results: ProbeResults) -> None:
    """Sample recent prioritization fees against the pump.fun program itself."""
    try:
        samples = await rpc.call(
            "getRecentPrioritizationFees", [[str(PUMP_FUN_PROGRAM_ID)]]
        )
    except DailyLimitReachedError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("getRecentPrioritizationFees failed: %s", exc)
        return
    for s in samples:
        results.fee_samples_lamports.append(s["prioritizationFee"])


def evaluate_fee_drag(settings, results: ProbeResults) -> None:
    if not results.fee_samples_lamports:
        return
    position_sol = settings.config.trading.position_sol
    max_fee_fraction = settings.config.fees.max_fee_fraction
    max_fee_absolute = settings.config.fees.max_fee_absolute_sol
    priority_fee_ceiling_sol = settings.config.fees.priority_fee_ceiling_sol
    ata_rent_sol = 0.00204

    for fee_lamports in results.fee_samples_lamports:
        # The real executor caps what it actually pays at priority_fee_ceiling_sol;
        # modeling the raw uncapped sample here would overstate rejections whenever
        # network fees spike above that ceiling.
        priority_fee_sol = min(fee_lamports / 1_000_000_000, priority_fee_ceiling_sol)
        est_roundtrip_cost = (
            2 * priority_fee_sol + 0.02 * position_sol + ata_rent_sol * 0.05
        )  # 0.05 = assumed rare-failed-close amortized cost
        results.fee_drag_candidates += 1
        fraction = est_roundtrip_cost / position_sol
        if fraction > max_fee_fraction or est_roundtrip_cost > max_fee_absolute:
            results.fee_drag_rejections += 1
        else:
            results.tier1_survivors += 1


async def fetch_idl_summary(rpc: RpcClient) -> None:
    """Best-effort: fetch the Anchor IDL account and print what we can parse.

    Anchor stores a compressed IDL at a PDA derived from the program ID.
    Full decompression/parsing of the IDL JSON is left as a manual step if
    this fails -- the goal here is a printed artifact to eyeball, not a
    hard assertion.
    """
    idl_seed = b"anchor:idl"
    from solders.pubkey import Pubkey

    base, _ = Pubkey.find_program_address([idl_seed], PUMP_FUN_PROGRAM_ID)
    try:
        info = await rpc.call("getAccountInfo", [str(base), {"encoding": "base64"}])
    except DailyLimitReachedError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("IDL account fetch failed: %s", exc)
        print("IDL account: fetch failed, verify manually against an explorer/IDL repo")
        return
    if info is None or info.get("value") is None:
        print("IDL account: not found at expected PDA; verify ordering manually")
        return
    data_b64 = info["value"]["data"][0]
    raw = base64.b64decode(data_b64)
    print(f"IDL account found at {base}, {len(raw)} raw bytes (compressed/prefixed).")
    print("Decompress with zlib + Anchor's 8-byte-length prefix to get the IDL JSON,")
    print("then diff its `buy`/`sell` instruction account lists against program.py.")


async def fetch_recent_buy_accounts(rpc: RpcClient, results: ProbeResults) -> None:
    """Print the account list of one real recent `buy` tx for manual comparison."""
    sample_sig = None
    for obs in results.observations:
        if obs.mint:
            try:
                sigs = await rpc.call("getSignaturesForAddress", [obs.mint, {"limit": 20}])
            except DailyLimitReachedError:
                raise
            except Exception:  # noqa: BLE001
                continue
            # The mint's own `create` transaction also invokes the pump.fun
            # program (self-CPI), so the newest signature isn't necessarily a
            # buy -- skip the create itself and require a genuinely different
            # transaction before trusting its account ordering.
            for entry in sigs:
                if entry["signature"] != obs.signature:
                    sample_sig = entry["signature"]
                    break
        if sample_sig:
            break

    if not sample_sig:
        print("No buy transaction available yet to print account ordering from.")
        return

    tx = await rpc.call(
        "getTransaction",
        [sample_sig, {"maxSupportedTransactionVersion": 0, "encoding": "jsonParsed"}],
    )
    if tx is None:
        return
    instructions = tx["transaction"]["message"]["instructions"]
    for ix in instructions:
        if ix.get("programId") == str(PUMP_FUN_PROGRAM_ID):
            print(f"\nResolved account ordering from real tx {sample_sig}:")
            accounts = ix.get("accounts", [])
            for i, acct in enumerate(accounts):
                print(f"  [{i}] {acct}")
            return
    print("Sampled tx did not directly invoke the pump.fun program (may be a CPI); "
          "widen the search or inspect manually.")


def print_report(results: ProbeResults) -> None:
    print("\n" + "=" * 60)
    print("PHASE 0 PROBE REPORT")
    print("=" * 60)
    print(f"Creates observed: {results.creates_seen}")

    ranks = [o.buys_before_us for o in results.observations if o.buys_before_us is not None]
    if ranks:
        print(f"Buy-queue rank (buys landing before our notification), n={len(ranks)}:")
        print(f"  median = {statistics.median(ranks)}")
        print(f"  mean   = {statistics.mean(ranks):.1f}")
        print(f"  max    = {max(ranks)}")
        if statistics.median(ranks) > 3:
            print("  -> WARNING: consistently behind several buys. A 1.5x scalp means")
            print("     buying from people already exiting. Consider re-scoping.")
    else:
        print("Buy-queue rank: no resolvable mints yet (increase --hours)")

    if results.fee_samples_lamports:
        fees_sol = [f / 1e9 for f in results.fee_samples_lamports]
        print(f"\nPriority fee samples, n={len(fees_sol)}:")
        print(f"  median = {statistics.median(fees_sol):.6f} SOL")
        print(f"  mean   = {statistics.mean(fees_sol):.6f} SOL")
        print(f"  max    = {max(fees_sol):.6f} SOL")

    if results.fee_drag_candidates:
        rejection_rate = results.fee_drag_rejections / results.fee_drag_candidates
        print(f"\nFee-drag guard: {results.fee_drag_rejections}/{results.fee_drag_candidates} "
              f"samples rejected ({rejection_rate:.0%})")
        if rejection_rate > 0.9:
            print("  -> PHASE 0 FAILURE MODE: the guard would reject nearly everything.")
            print("     Do not build and run a bot that cannot trade. Either raise")
            print("     max_fee_fraction knowingly, raise position size, or accept that")
            print("     this configuration does not trade in current conditions.")
    print("=" * 60)


# Rank resolution costs up to 2 RPC calls per create (more with getTransaction
# retries). Past this many, keep counting creates (free, from PumpPortal) but
# stop spending RPC budget on rank -- a few hundred is plenty for a median/
# rejection-rate estimate, and this is what stands between a normal run and
# re-exhausting the daily cap.
MAX_RANK_RESOLUTIONS = 300

# Each resolution now waits out MIN_TX_AGE_S plus possible retries (up to
# ~40s), so resolving them one at a time would cap us well under
# MAX_RANK_RESOLUTIONS in an hour. Run a bounded number concurrently instead.
RESOLVER_CONCURRENCY = 15


async def main(hours: float | None) -> None:
    settings = load_settings()
    if hours is None:
        hours = settings.config.probe.default_hours
    results = ProbeResults()
    queue: asyncio.Queue[CreateObservation] = asyncio.Queue()
    daily_limit_hit = asyncio.Event()

    async with RpcClient(settings) as rpc:
        listener_task = asyncio.create_task(
            listen_for_creates(settings, results, hours * 3600, queue)
        )

        resolver_tasks: list[asyncio.Task] = []

        async def resolver_loop() -> None:
            resolved = 0
            sem = asyncio.Semaphore(RESOLVER_CONCURRENCY)

            async def run_one(obs: CreateObservation) -> None:
                async with sem:
                    try:
                        await resolve_mint_and_rank(rpc, obs)
                    except DailyLimitReachedError as exc:
                        logger.error(
                            "%s -- halting RPC-based checks for the rest of this run", exc
                        )
                        daily_limit_hit.set()

            while True:
                obs = await queue.get()
                if resolved >= MAX_RANK_RESOLUTIONS or daily_limit_hit.is_set():
                    continue
                resolved += 1
                resolver_tasks.append(asyncio.create_task(run_one(obs)))

        resolver_task = asyncio.create_task(resolver_loop())

        # Sample fees periodically for the duration of the run. Each call
        # covers ~150 recent slots (~60s of chain time), so sampling faster
        # than 60s just burns quota on overlapping data.
        async def fee_sampler_loop() -> None:
            while not listener_task.done() and not daily_limit_hit.is_set():
                try:
                    await sample_priority_fees(rpc, results)
                except DailyLimitReachedError as exc:
                    logger.error("%s -- halting fee sampling for the rest of this run", exc)
                    daily_limit_hit.set()
                    break
                await asyncio.sleep(60)

        fee_task = asyncio.create_task(fee_sampler_loop())

        await listener_task
        # Let in-flight resolutions drain -- each can take up to MIN_TX_AGE_S
        # plus GET_TX_RETRIES * GET_TX_RETRY_DELAY_S (~40s), not the few
        # seconds a bare RPC round-trip would need.
        resolver_task.cancel()
        drain_deadline = time.monotonic() + MIN_TX_AGE_S + GET_TX_RETRIES * GET_TX_RETRY_DELAY_S
        pending = [t for t in resolver_tasks if not t.done()]
        while pending and time.monotonic() < drain_deadline:
            await asyncio.sleep(1)
            pending = [t for t in pending if not t.done()]
        for t in pending:
            t.cancel()
        fee_task.cancel()

        evaluate_fee_drag(settings, results)
        if daily_limit_hit.is_set():
            print("\nQuickNode daily request limit was hit during this run -- IDL/account-")
            print("ordering checks and any fee samples after that point were skipped.")
        else:
            print("\nFetching IDL for manual account-ordering comparison...")
            try:
                await fetch_idl_summary(rpc)
                await fetch_recent_buy_accounts(rpc, results)
            except DailyLimitReachedError as exc:
                print(f"Skipped: {exc}")

    print_report(results)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase 0 gate for pumpbot")
    parser.add_argument("--hours", type=float, default=None)
    args = parser.parse_args()
    asyncio.run(main(args.hours))
