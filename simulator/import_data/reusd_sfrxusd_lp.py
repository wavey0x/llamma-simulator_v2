#!/usr/bin/env python3
"""Collect frozen Ethereum observations or prepare paired Spot and oracle prices.

Use collect with ETH_RPC_URL in the environment, or prepare with --history for
an offline rebuild. Output is gzip JSON Lines: a metadata header, then records.
Prices are WAD integers: Spot is crvUSD/LP and the three-leg oracle is USD/LP.
The target oracle and its price_w schedule are reconstructed assumptions, not
a historical deployment. This script does not run the AMM or select parameters.
"""
import argparse
import asyncio
import gzip
import hashlib
import json
import math
import os
import platform
from dataclasses import asdict, dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from eth_abi import decode, encode
from eth_utils import keccak

ROOT = Path(__file__).resolve().parents[2]
A_PRECISION = 10**4
WAD = 10**18
POOL = "0xed785af60bed688baa8990cd5c4166221599a441"
BRIDGE = "0xc522a6606bba746d7960404f22a3db936b6f4f50"
FEED = "0x07ac1e016d4335fb833666ed5c43846162d2b7e8"
AGG = "0x18672b1b0c623a30089a280ed9256379fb0e4e62"
REGISTRY = "0x10101010e0c3171d894b71b3400668af311e7d94"
MULTICALL = "0xca11bde05977b3631167028862be2a173976ca11"


def calldata(signature, types=(), args=()):
    return "0x" + (keccak(text=signature)[:4] + encode(types, args)).hex()


POOL_CALLS = [
    ("timestamp", MULTICALL, calldata("getCurrentBlockTimestamp()")),
    ("lp_A_precise", POOL, calldata("A_precise()")),
    ("lp_virtual_price", POOL, calldata("get_virtual_price()")),
    ("lp_last_price", POOL, calldata("last_price(uint256)", ["uint256"], [0])),
    ("lp_price_oracle", POOL, calldata("price_oracle(uint256)", ["uint256"], [0])),
    ("bridge_last_price", BRIDGE, calldata("last_price(uint256)", ["uint256"], [0])),
    ("bridge_price_oracle", BRIDGE, calldata("price_oracle(uint256)", ["uint256"], [0])),
]
FEED_CALLS = [
    ("redemption_handler", REGISTRY, calldata("redemptionHandler()")),
    ("reusd_feed", FEED, calldata("priceAsCrvusd()")),
    ("crvusd_agg_price", AGG, calldata("price()")),
]


def aggregate(calls):
    return calldata(
        "aggregate3((address,bool,bytes)[])",
        ["(address,bool,bytes)[]"],
        [[(address, True, bytes.fromhex(data[2:])) for _, address, data in calls]],
    )


def read_history(path):
    with gzip.open(path, "rt") as stream:
        metadata = json.loads(next(stream))["metadata"]
        rows = [json.loads(line) for line in stream]
    if len(rows) != metadata["record_count"]:
        raise ValueError("history record count mismatch")
    if metadata.get("schema") in (3, 4):
        if not rows or metadata["chain_id"] != 1:
            raise ValueError("empty or non-Ethereum history")
        expected = sample_blocks(
            metadata["first_block"], metadata["last_block"], metadata["block_step"], metadata.get("dense_ranges", [])
        )
        if [r["block"] for r in rows] != expected:
            raise ValueError("missing or duplicate sampled block")
        if (
            rows[-1]["block_hash"] != metadata["pin"]["block_hash"]
            or rows[0]["timestamp"] != metadata["first_timestamp"]
            or rows[-1]["timestamp"] != metadata["last_timestamp"]
        ):
            raise ValueError("history anchor mismatch")
        for row in rows:
            if metadata["schema"] == 4 and row.get("crvusd_agg_price", 0) <= 0:
                raise ValueError("missing or invalid USD aggregator price")
            if len(row["block_hash"]) != 66 or int(row["block_hash"], 16) == 0:
                raise ValueError("missing block hash")
            if (row["reusd_feed"] is None) != (row["block"] < metadata["feed_first_available_block"]):
                raise ValueError("missing deployed feed or unexpected predeployment answer")
    return metadata, rows


def sample_blocks(start, end, step, dense_ranges=()):
    if step < 1 or start > end:
        raise ValueError("invalid sampling range")
    blocks = set(range(start, end + 1, step))
    for first, last in dense_ranges:
        if not start <= first <= last <= end:
            raise ValueError("dense range must be inside the collection range")
        blocks.update(range(first, last + 1))
    return sorted(blocks)


def write_history(path, metadata, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".partial")
    with temporary.open("wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as stream:
        stream.write((json.dumps({"metadata": metadata}, sort_keys=True) + "\n").encode())
        for record in rows:
            stream.write((json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode())
    temporary.replace(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def collect(args):
    import aiohttp

    url = os.environ["ETH_RPC_URL"]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:

        async def batch(requests):
            payload = [
                {"jsonrpc": "2.0", "id": i, "method": method, "params": params}
                for i, (method, params) in enumerate(requests)
            ]
            for attempt in range(4):
                try:
                    async with session.post(url, json=payload) as response:
                        response.raise_for_status()
                        items = await response.json()
                    by_id = {item["id"]: item for item in items}
                    if len(by_id) != len(payload) or any("error" in item for item in items):
                        raise ValueError("incomplete or failed RPC batch")
                    return [by_id[i]["result"] for i in range(len(payload))]
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, TypeError):
                    if attempt == 3:
                        raise RuntimeError("RPC batch failed; no missing observation was substituted") from None
                    await asyncio.sleep(2**attempt)

        async def rpc(method, params):
            return (await batch([(method, params)]))[0]

        def call(address, data, block):
            return "eth_call", [{"to": address, "data": data}, hex(block)]

        if int(await rpc("eth_chainId", []), 16) != 1:
            raise ValueError("Ethereum mainnet required")
        blocks = sample_blocks(args.start_block, args.end_block, args.block_step, args.dense_range)
        start, end = blocks[0], blocks[-1]
        pin = await rpc("eth_getBlockByNumber", [hex(end), False])
        # An absent predeployment feed is expected; a failed deployed feed is not.
        lo, hi = start - 1, end
        if await rpc("eth_getCode", [FEED, hex(end)]) == "0x":
            raise ValueError("end block precedes feed deployment")
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if await rpc("eth_getCode", [FEED, hex(mid)]) == "0x":
                lo = mid
            else:
                hi = mid
        feed_start = hi
        base, provenance = {}, None
        verify_blocks = set(blocks[:: max(1, len(blocks) // 32)] + [end])
        if args.reuse:
            meta, records = read_history(args.reuse)
            anchor = await rpc("eth_getBlockByNumber", [hex(meta["pin"]["block_number"]), False])
            if meta["chain_id"] != 1 or anchor["hash"] != meta["pin"]["block_hash"]:
                raise ValueError("reused history chain anchor mismatch")
            if meta["contracts"]["lp_pool"].lower() != POOL or meta["contracts"]["bridge_pool"].lower() != BRIDGE:
                raise ValueError("reused history has different pools")
            base = {row["block"]: row for row in records}
            if len(base) != len(records):
                raise ValueError("duplicate reused block")
            provenance = {
                "sha256": hashlib.sha256(args.reuse.read_bytes()).hexdigest(),
                "pin": meta["pin"],
                "verification_blocks": sorted(verify_blocks),
            }

        semaphore = asyncio.Semaphore(args.workers)
        cache_key = hashlib.sha256(
            json.dumps(
                [4, start, end, args.block_step, args.dense_range, pin["hash"], provenance], sort_keys=True
            ).encode()
        ).hexdigest()[:16]
        cache = ROOT / ".tmp" / "reusd-collection" / cache_key
        cache.mkdir(parents=True, exist_ok=True)

        async def chunk(numbers):
            cached = cache / f"{numbers[0]}.json"
            if cached.exists():
                return json.loads(cached.read_text())
            async with semaphore:
                requests, layouts = [], []
                for block in numbers:
                    calls = POOL_CALLS + FEED_CALLS if block not in base or block in verify_blocks else FEED_CALLS
                    layouts.append(calls)
                    requests.append(call(MULTICALL, aggregate(calls), block))
                    # BLOCKHASH at b+1 returns b's hash without downloading its transactions.
                    requests.append(call(MULTICALL, calldata("getBlockHash(uint256)", ["uint256"], [block]), block + 1))
                results = await batch(requests)
                rows = []
                for i, (block, calls) in enumerate(zip(numbers, layouts)):
                    row = {field: base[block][field] for field, _, _ in POOL_CALLS} if block in base else {}
                    row.update(block=block, block_hash=results[2 * i + 1])
                    if len(row["block_hash"]) != 66 or int(row["block_hash"], 16) == 0:
                        raise ValueError("missing block hash")
                    values = decode(["(bool,bytes)[]"], bytes.fromhex(results[2 * i][2:]))[0]
                    if len(values) != len(calls):
                        raise ValueError("incomplete multicall")
                    for (field, _, _), (success, value) in zip(calls, values):
                        if field == "reusd_feed" and block < feed_start:
                            if value:
                                raise ValueError("unexpected predeployment feed answer")
                            row[field] = None
                            continue
                        if not success or len(value) != 32:
                            raise ValueError(f"failed {field} at block {block}")
                        value = "0x" + value[-20:].hex() if field == "redemption_handler" else int.from_bytes(value)
                        if field in row and row[field] != value:
                            raise ValueError(f"reused {field} disagrees at block {block}")
                        row[field] = value
                    if row["crvusd_agg_price"] <= 0:
                        raise ValueError(f"invalid USD aggregator price at block {block}")
                    rows.append(row)
                fees = await batch(
                    [call(row["redemption_handler"], calldata("baseRedemptionFee()"), row["block"]) for row in rows]
                )
                for row, raw_fee in zip(rows, fees):
                    if len(raw_fee) != 66:
                        raise ValueError("missing base redemption fee")
                    row["base_redemption_fee"] = int(raw_fee, 16)
                    if not 0 <= row["base_redemption_fee"] <= WAD:
                        raise ValueError("invalid base redemption fee")
                    modeled = max(WAD * WAD // row["bridge_price_oracle"], WAD - row["base_redemption_fee"])
                    if row["reusd_feed"] is not None and row["reusd_feed"] != modeled:
                        raise ValueError(f"feed formula mismatch at block {row['block']}")
                temporary = cached.with_suffix(".partial")
                temporary.write_text(json.dumps(rows, separators=(",", ":")))
                temporary.replace(cached)
                return rows

        tasks = [asyncio.create_task(chunk(blocks[i : i + 100])) for i in range(0, len(blocks), 100)]
        rows = []
        try:
            for count, task in enumerate(asyncio.as_completed(tasks), 1):
                rows.extend(await task)
                if count % 100 == 0 or count == len(tasks):
                    print(f"Collected {count}/{len(tasks)} chunks", flush=True)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
        rows.sort(key=lambda row: row["block"])
        if [row["block"] for row in rows] != blocks:
            raise ValueError("incomplete collection")
        if (await rpc("eth_getBlockByNumber", [hex(end), False]))["hash"] != pin["hash"]:
            raise ValueError("chain changed during collection")
        metadata = {
            "schema": 4,
            "chain_id": 1,
            "block_step": args.block_step,
            "dense_ranges": args.dense_range,
            "record_count": len(rows),
            "first_block": start,
            "last_block": end,
            "first_timestamp": rows[0]["timestamp"],
            "last_timestamp": rows[-1]["timestamp"],
            "feed_first_available_block": feed_start,
            "pin": {"block_number": end, "block_hash": pin["hash"], "block_timestamp": int(pin["timestamp"], 16)},
            "contracts": {
                "lp_pool": POOL,
                "bridge_pool": BRIDGE,
                "reusd_feed": FEED,
                "registry": REGISTRY,
                "crvusd_aggregator": AGG,
            },
            "reused_history": provenance,
            "units": "prices and fees WAD; LP A uses pool A_precise scaling",
        }
        return metadata, rows


@dataclass(frozen=True)
class Config:
    ema_seconds: int = 866
    oracle_update_seconds: int = 0  # price_w at every recorded block
    initial_vp_scale: float = 1.0


def _x_from_y(a_raw, y):
    b = WAD - 4 * a_raw * (WAD - y) // A_PRECISION
    radicand = b * b + 4 * a_raw * WAD**3 // (A_PRECISION * y)
    return (math.isqrt(radicand) - b) * A_PRECISION // (8 * a_raw)


def _p_from_y(a_raw, y):
    x = _x_from_y(a_raw, y)
    a = 4 * a_raw * x // A_PRECISION
    return (a + WAD**3 // (4 * y * y)) * WAD // (a + WAD**3 // (4 * x * y))


@lru_cache(maxsize=65536)
def portfolio_value(a_raw, price):
    """Integer port of pinned curve-std lp_oracle_2 (coin zero).

    The pool's A_precise must be rescaled before calling this function.
    Keep rounding and the 1e-6 bisection price tolerance identical to Vyper.
    """
    if not (0 < a_raw <= 100000 * A_PRECISION and 10**16 <= price <= 10**20):
        raise ValueError("outside the contract's LP solver domain")
    target = (WAD * WAD + price // 2) // price if price < WAD else price
    lo, hi = 1, WAD // 2 + 1
    for _ in range(60):
        y = (lo + hi) // 2
        candidate = _p_from_y(a_raw, y)
        if abs(candidate - target) <= target // 10**6:
            break
        if candidate > target:
            lo = y
        else:
            hi = y
        if hi - lo <= 1:
            y = hi
            break
    else:
        raise ArithmeticError("LP solver did not converge")
    x = _x_from_y(a_raw, y)
    if price < WAD:
        x, y = y, x
    return x + price * y // WAD


@lru_cache(maxsize=8192)
def decay(dt, horizon):
    # Float exp is the only approximation to the contract's integer EMA math.
    return int(math.exp(-dt / horizon) * WAD)


class VirtualPriceEMA:
    def __init__(self, value, timestamp, horizon):
        self.previous = self.queued = value
        self.timestamp, self.horizon = timestamp, horizon

    def price(self, spot, timestamp, write=True):
        if timestamp < self.timestamp or self.horizon <= 0:
            raise ValueError("invalid EMA time")
        mul = decay(timestamp - self.timestamp, self.horizon)
        smoothed = (self.previous * mul + self.queued * (WAD - mul)) // WAD
        value = min(spot, smoothed)
        if write:
            self.previous = value
            self.queued = spot
            self.timestamp = timestamp
        return value


def feed_value(row):
    fee = row["base_redemption_fee"]
    if not 0 <= fee <= WAD or row["bridge_price_oracle"] <= 0:
        raise ValueError("invalid redemption inputs")
    modeled = max(WAD * WAD // row["bridge_price_oracle"], WAD - fee)
    observed = row["reusd_feed"]
    if observed is not None and observed != modeled:
        raise ValueError("observed feed disagrees with proposed reconstruction")
    return min(WAD, modeled if observed is None else observed)


def reconstruct(rows, config):
    if not rows:
        raise ValueError("empty history")
    ema = VirtualPriceEMA(
        int(Decimal(rows[0]["lp_virtual_price"]) * Decimal(str(config.initial_vp_scale))),
        rows[0]["timestamp"],
        config.ema_seconds,
    )
    points = []
    previous_block, previous_time = -1, -1
    for row in rows:
        timestamp, block = row["timestamp"], row["block"]
        if block <= previous_block or timestamp <= previous_time:
            raise ValueError("history must be strictly chronological")
        previous_block, previous_time = block, timestamp
        a_raw = row["lp_A_precise"] * A_PRECISION // 200
        vp = row["lp_virtual_price"]
        write = timestamp - ema.timestamp >= config.oracle_update_seconds
        smoothed = ema.price(vp, timestamp, write)
        lp_spot = portfolio_value(a_raw, row["lp_last_price"]) * vp // WAD
        lp_oracle = portfolio_value(a_raw, row["lp_price_oracle"]) * smoothed // WAD
        spot = lp_spot * (WAD * WAD // row["bridge_last_price"]) // WAD
        oracle = lp_oracle * feed_value(row) // WAD
        # Match ChainOracle's third leg and stepwise integer rounding. LLAMMA
        # consumes this USD-valued number directly; market trades remain crvUSD.
        aggregator = row["crvusd_agg_price"]
        if aggregator <= 0:
            raise ValueError("nonpositive USD aggregator price")
        oracle = oracle * aggregator // WAD
        if spot <= 0 or oracle <= 0:
            raise ValueError("nonpositive price")
        points.append((timestamp, block, spot, oracle))
    return points


def prepare(history: Path, output: Path):
    if output.exists():
        raise FileExistsError("Output already exists; use a new path to preserve frozen data")
    metadata, rows = read_history(history)
    if metadata.get("schema") != 4 or metadata.get("chain_id") != 1:
        raise ValueError("Expected schema-4 Ethereum history including the USD aggregator")
    config = Config()
    points = reconstruct(rows, config)
    header = {
        "schema": "reusd-sfrxusd-lp-prices-v1",
        "record_count": len(points),
        "history": metadata,
        "history_sha256": hashlib.sha256(history.read_bytes()).hexdigest(),
        "code_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (Path(__file__), ROOT / "uv.lock")},
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "config": asdict(config),
        "target_source": "https://github.com/wavey0x/curve-stablecoin/tree/67b3b3b4057bf5d5f99b128b4b49cc9e2eae5f1a",
        "scale": WAD,
        "units": {"spot": "crvUSD per LP", "oracle": "USD per LP", "timestamp": "Unix seconds"},
        "oracle_state": "Initialized from the first LP virtual price; continuous price_w at each recorded block",
        "quote_convention": "Keep the USD aggregator in Oracle; Spot is independently valued in crvUSD. No alignment conversion.",
        "limitations": [
            "Point snapshots, not OHLC; denser sampling in recorded stress ranges",
            "The target oracle and its write schedule are counterfactual",
            "EMA exponentiation uses floating point; other price arithmetic uses integers",
            "All observations retained; loan eligibility and warm-up exclusions belong to the experiment",
        ],
    }
    records = ({"timestamp": t, "block": b, "spot": spot, "oracle": oracle} for t, b, spot, oracle in points)
    return write_history(output, header, records)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    collect_parser = commands.add_parser("collect", help="freeze on-chain observations using ETH_RPC_URL")
    collect_parser.add_argument("--start-block", type=int, default=22_088_660)
    collect_parser.add_argument("--end-block", type=int, required=True)
    collect_parser.add_argument("--block-step", type=int, default=5)
    collect_parser.add_argument(
        "--dense-range",
        type=lambda s: tuple(map(int, s.split(":"))),
        action="append",
        default=[],
        help="inclusive START:END range sampled every block; repeatable",
    )
    collect_parser.add_argument("--workers", type=int, default=8)
    collect_parser.add_argument("--reuse", type=Path, help="enrich and verify pinned legacy pool readings")
    collect_parser.add_argument("--output", type=Path, required=True)
    prepare_parser = commands.add_parser("prepare", help="rebuild prices offline from frozen history")
    prepare_parser.add_argument("--history", type=Path, required=True)
    prepare_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists; use a new path to preserve frozen data")
    if args.command == "collect":
        if args.block_step < 1 or args.workers < 1 or args.end_block < args.start_block:
            collect_parser.error("invalid block range, step, or worker count")
        meta, rows = asyncio.run(collect(args))
        digest = write_history(args.output, meta, rows)
    else:
        digest = prepare(args.history, args.output)
    print(json.dumps({"sha256": digest}, sort_keys=True))
