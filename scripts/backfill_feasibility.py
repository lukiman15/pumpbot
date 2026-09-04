"""BOT-SHARE-HANDOFF Task 1 -- feasibility gate for backfilling bonding-curve
transaction history. Must be run and its result respected (stop if it fails)
before any full backfill (scripts/backfill_trades.py) is attempted. See
.claude/plans/BOT-SHARE-HANDOFF.md Section 3 for the full rationale.

Picks 20 mints at random (fixed seed, recorded) from the 4c10b851... run's
ShadowPrice events, derives each mint's bonding curve PDA (reusing
program.derive_bonding_curve_pda -- not a new derivation), and pages through
getSignaturesForAddress oldest-to-newest to see whether history reaching back
to before the mint's first ShadowPrice observation is still retained by the
RPC provider.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from solders.pubkey import Pubkey

from pumpbot.config import PROJECT_ROOT, load_settings
from pumpbot.ledger import read_events
from pumpbot.program import derive_bonding_curve_pda
from pumpbot.rpc import RpcClient

NEW_RUN_ID = "4c10b851d9824d24823f359e860c699c"
SAMPLE_SIZE = 20
GATE_THRESHOLD = 15
RANDOM_SEED = 20260904  # fixed, recorded per Section 3.1 item 1


@dataclass
class MintFeasibility:
    mint: str
    first_shadow_ts_wall: float
    signature_count: int
    oldest_signature_block_time: float | None
    oldest_signature_predates_first_shadow: bool | None
    error: str | None


def pick_sample(ledger_dir: Path, run_id: str, sample_size: int, seed: int) -> list[tuple[str, float]]:
    """Returns (mint, first_shadow_ts_wall) pairs, earliest ShadowPrice per mint.
    Uses ts_wall (unix epoch), not ts_monotonic (process-relative and not
    comparable to a transaction's on-chain blockTime)."""
    first_seen: dict[str, float] = {}
    for row in read_events(ledger_dir):
        if row.get("run_id") != run_id or row.get("event") != "ShadowPrice":
            continue
        mint = row["mint"]
        ts = row["ts_wall"]
        if mint not in first_seen or ts < first_seen[mint]:
            first_seen[mint] = ts

    mints = sorted(first_seen.items())  # sort first for reproducible sampling order
    rng = random.Random(seed)
    sample = rng.sample(mints, min(sample_size, len(mints)))
    return sample


async def fetch_all_signatures(
    client: RpcClient, address: str
) -> list[dict]:
    """Pages getSignaturesForAddress oldest-to-newest via the `before` cursor,
    walking backward from the most recent signature until exhausted. Uses the
    shadow pool so a concurrently running bot's trading calls are unaffected."""
    all_sigs: list[dict] = []
    before: str | None = None
    while True:
        params: list = [address, {"limit": 1000}]
        if before is not None:
            params[1]["before"] = before
        page = await client.call("getSignaturesForAddress", params, pool="shadow")
        if not page:
            break
        all_sigs.extend(page)
        if len(page) < 1000:
            break
        before = page[-1]["signature"]
    all_sigs.reverse()  # oldest to newest
    return all_sigs


async def check_one(client: RpcClient, mint: str, first_shadow_ts_wall: float) -> MintFeasibility:
    try:
        pda = derive_bonding_curve_pda(Pubkey.from_string(mint))
        sigs = await fetch_all_signatures(client, str(pda))
    except Exception as exc:  # noqa: BLE001 -- feasibility probe must not crash on one bad mint
        return MintFeasibility(mint, first_shadow_ts_wall, 0, None, None, f"{type(exc).__name__}: {exc}")

    if not sigs:
        return MintFeasibility(mint, first_shadow_ts_wall, 0, None, None, None)

    oldest_block_time = sigs[0].get("blockTime")
    # "Predates the mint's first ShadowPrice observation" per Section 3.1
    # item 4: the oldest returned signature's blockTime (unix epoch, same
    # scale as ts_wall) must be at or before first_shadow_ts_wall. A small
    # positive slack is allowed for clock/poll-cadence noise -- shadow
    # tracking begins on CandidateSeen, which is itself slightly after the
    # mint's creation transaction, so the true earliest trade can be a few
    # seconds before the first shadow poll even with complete retention.
    predates = (
        oldest_block_time is not None and oldest_block_time <= first_shadow_ts_wall + 5.0
    )
    return MintFeasibility(
        mint=mint,
        first_shadow_ts_wall=first_shadow_ts_wall,
        signature_count=len(sigs),
        oldest_signature_block_time=oldest_block_time,
        oldest_signature_predates_first_shadow=predates,
        error=None,
    )


async def main_async(args: argparse.Namespace) -> None:
    settings = load_settings()
    ledger_dir = Path(args.ledger) if args.ledger else (PROJECT_ROOT / settings.config.ledger.path).parent

    sample = pick_sample(ledger_dir, NEW_RUN_ID, SAMPLE_SIZE, RANDOM_SEED)
    print(f"Random seed: {RANDOM_SEED}")
    print(f"Sample size: {len(sample)} mints from run_id={NEW_RUN_ID}")
    print()

    results: list[MintFeasibility] = []
    async with RpcClient(settings) as client:
        for i, (mint, first_ts) in enumerate(sample, start=1):
            result = await check_one(client, mint, first_ts)
            results.append(result)
            if result.error is not None:
                status = f"ERROR({result.error})"
            elif result.signature_count == 0:
                status = "EMPTY"
            elif result.oldest_signature_predates_first_shadow:
                status = "COMPLETE"
            else:
                status = "TRUNCATED"
            print(f"[{i}/{len(sample)}] mint=...{mint[-8:]} sigs={result.signature_count} {status}")

    n_complete = sum(1 for r in results if r.oldest_signature_predates_first_shadow)
    n_empty = sum(1 for r in results if r.error is None and r.signature_count == 0)
    n_truncated = sum(
        1 for r in results if r.error is None and r.signature_count > 0 and not r.oldest_signature_predates_first_shadow
    )
    n_error = sum(1 for r in results if r.error is not None)

    print()
    print("=== Feasibility summary ===")
    print(f"  mints with history reaching back before first ShadowPrice: {n_complete}/{len(sample)}")
    print(f"  mints with history present but truncated after first ShadowPrice: {n_truncated}/{len(sample)}")
    print(f"  mints with empty history (no error, zero signatures): {n_empty}/{len(sample)}")
    print(f"  mints with an RPC error: {n_error}/{len(sample)}")
    if n_error:
        for r in results:
            if r.error:
                print(f"    {r.mint}: {r.error}")

    total_sigs = sum(r.signature_count for r in results)
    mean_sigs = total_sigs / len(sample) if sample else 0.0
    print(f"  mean signatures/mint (over full sample, incl. empties): {mean_sigs:.1f}")

    gate_pass = n_complete >= GATE_THRESHOLD
    print()
    print(f"=== Gate: {'PASS' if gate_pass else 'FAIL'} ({n_complete}/{len(sample)} >= {GATE_THRESHOLD} required) ===")

    out_path = PROJECT_ROOT / "data" / "backfill_feasibility_sample.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {
                "seed": RANDOM_SEED,
                "run_id": NEW_RUN_ID,
                "gate_pass": gate_pass,
                "n_complete": n_complete,
                "n_truncated": n_truncated,
                "n_empty": n_empty,
                "n_error": n_error,
                "mean_signatures_per_mint": mean_sigs,
                "results": [asdict(r) for r in results],
            },
            indent=2,
        )
    )
    print(f"Raw sample results (mint addresses -- not for the report) written to {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="BOT-SHARE-HANDOFF Task 1 feasibility gate")
    parser.add_argument("--ledger", type=str, default=None)
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
