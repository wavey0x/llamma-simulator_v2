"""Collect uniform block evidence; credentials are read only from the environment.

Run as python -m simulator.import_data.reusd_sfrxusd_lp_blocks. Cached chunks are
bound to the chain anchor and source history; only a complete collection is
published. No price or oracle reconstruction takes place in this module.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import os
import time
from pathlib import Path

import aiohttp
from eth_abi import decode

from simulator.import_data import reusd_sfrxusd_lp as source


def sha(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def endpoint(provider):
    keys = {
        "alchemy": ("WEB3_ALCHEMY_PROJECT_ID", "https://eth-mainnet.g.alchemy.com/v2/"),
        "infura": ("WEB3_INFURA_PROJECT_ID", "https://mainnet.infura.io/v3/"),
    }
    if provider == "configured":
        value = os.environ.get("ETH_RPC_URL")
    else:
        key, prefix = keys[provider]
        value = prefix + os.environ[key] if os.environ.get(key) else None
    if not value:
        raise ValueError("Archive RPC configuration is unavailable")
    return value


class RPC:
    def __init__(self, session, url, batch_size=20, rate=80):
        self.session, self.url = session, url
        self.batch_size, self.rate = batch_size, rate
        self.calls = self.retries = 0
        self.lock = asyncio.Lock()
        self.next_request = 0.0

    async def batch(self, requests):
        # Provider batch limits are independent of the collection chunk size.
        if len(requests) > self.batch_size:
            result = []
            for start in range(0, len(requests), self.batch_size):
                result.extend(await self.batch(requests[start : start + self.batch_size]))
            return result
        payload = [
            dict(jsonrpc="2.0", id=i, method=method, params=params) for i, (method, params) in enumerate(requests)
        ]
        failure = "Unknown RPC failure"
        for attempt in range(6):
            async with self.lock:
                await asyncio.sleep(max(0, self.next_request - time.monotonic()))
                self.next_request = time.monotonic() + len(payload) / self.rate
            self.calls += len(payload)
            try:
                async with self.session.post(self.url, json=payload) as response:
                    if response.status != 200:
                        raise ValueError(f"RPC HTTP status {response.status}")
                    records = await response.json()
                if not isinstance(records, list):
                    raise ValueError("RPC batch response is not a list")
                if any("id" not in item for item in records):
                    raise ValueError(
                        "RPC unkeyed failure codes " + ",".join(sorted({str(item.get("code")) for item in records}))
                    )
                by_id = {item["id"]: item for item in records}
                if len(records) != len(payload) or set(by_id) != set(range(len(payload))):
                    raise ValueError("Incomplete RPC batch")
                if any("error" in item or "result" not in item for item in records):
                    codes = sorted({str(item.get("error", {}).get("code")) for item in records if "error" in item})
                    raise ValueError("RPC method failure codes " + ",".join(codes))
                return [by_id[i]["result"] for i in range(len(payload))]
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, TypeError) as exc:
                failure = str(exc) if type(exc) is ValueError else type(exc).__name__
                if attempt == 5:
                    raise RuntimeError("Archive RPC batch failed after retries: " + failure) from None
                self.retries += 1
                await asyncio.sleep(min(2**attempt, 10))

    async def one(self, method, params):
        return (await self.batch([(method, params)]))[0]


def call(address, data, block):
    return "eth_call", [{"to": address, "data": data}, hex(block)]


def validate_row(row, feed_start):
    if row["timestamp"] <= 0 or len(row["block_hash"]) != 66 or int(row["block_hash"], 16) == 0:
        raise ValueError("Invalid block identity")
    for key in (
        "lp_A_precise",
        "lp_virtual_price",
        "lp_last_price",
        "lp_price_oracle",
        "bridge_last_price",
        "bridge_price_oracle",
        "crvusd_agg_price",
    ):
        if row[key] <= 0:
            raise ValueError(f"Invalid historical {key}")
    if (row["reusd_feed"] is None) != (row["block"] < feed_start):
        raise ValueError("Missing deployed feed or unexpected predeployment answer")
    if not 0 <= row["base_redemption_fee"] <= source.WAD:
        raise ValueError("Invalid historical redemption fee")
    modeled = max(source.WAD**2 // row["bridge_price_oracle"], source.WAD - row["base_redemption_fee"])
    if row["reusd_feed"] is not None and row["reusd_feed"] != modeled:
        raise ValueError("Observed feed differs from the pinned reconstruction")


async def fetch_rows(rpc, blocks, feed_start):
    calls = source.POOL_CALLS + source.FEED_CALLS
    requests = []
    for block in blocks:
        requests.append(call(source.MULTICALL, source.aggregate(calls), block))
        requests.append(
            call(source.MULTICALL, source.calldata("getBlockHash(uint256)", ["uint256"], [block]), block + 1)
        )
    values = await rpc.batch(requests)
    rows = []
    for i, block in enumerate(blocks):
        row = dict(block=block, block_hash=values[2 * i + 1])
        decoded = decode(["(bool,bytes)[]"], bytes.fromhex(values[2 * i][2:]))[0]
        if len(decoded) != len(calls):
            raise ValueError("Incomplete historical multicall")
        for (name, _, _), (success, value) in zip(calls, decoded):
            if name == "reusd_feed" and block < feed_start:
                if value:
                    raise ValueError("Unexpected predeployment feed answer")
                row[name] = None
            else:
                if not success or len(value) != 32:
                    raise ValueError(f"Failed historical {name} at block {block}")
                row[name] = "0x" + value[-20:].hex() if name == "redemption_handler" else int.from_bytes(value)
        rows.append(row)
    fees = await rpc.batch(
        [call(r["redemption_handler"], source.calldata("baseRedemptionFee()"), r["block"]) for r in rows]
    )
    for row, fee in zip(rows, fees, strict=True):
        if not isinstance(fee, str) or len(fee) != 66:
            raise ValueError("Missing historical redemption fee")
        row["base_redemption_fee"] = int(fee, 16)
        validate_row(row, feed_start)
    return rows


async def collect(args):
    history, cache, output = Path(args.history), Path(args.cache), Path(args.output)
    if output.exists():
        raise FileExistsError("Frozen history already exists")
    with gzip.open(history, "rt") as stream:
        meta = json.loads(next(stream))["metadata"]
    source_hash = sha(history)
    if source_hash != args.source_sha256:
        raise ValueError("Frozen source history fingerprint differs")
    first, last = meta["first_block"], meta["last_block"]
    stop = min(last, first + args.pilot_blocks - 1) if args.pilot_blocks else last
    identity = dict(
        schema="uniform-blocks-v1",
        source_sha256=source_hash,
        pin=meta["pin"],
        first_block=first,
        last_block=last,
        collector_sha256=sha(__file__),
    )
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    cache = cache / key
    cache.mkdir(parents=True, exist_ok=True)
    write(cache / "identity.json", identity)
    started = time.monotonic()
    stats = dict(
        status="collecting",
        expected_blocks=stop - first + 1,
        complete_blocks=0,
        reused_blocks=0,
        new_blocks=0,
        cached_blocks=0,
        verified_reuse=0,
    )
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
        rpc = RPC(session, endpoint(args.provider), args.batch_size, args.rate)
        if int(await rpc.one("eth_chainId", []), 16) != 1:
            raise ValueError("Ethereum mainnet required")
        pin = await rpc.one("eth_getBlockByNumber", [hex(last), False])
        if pin["hash"] != meta["pin"]["block_hash"]:
            raise ValueError("Historical chain anchor differs")
        verify_stride = max(1, (last - first) // 32)
        verify = {first + i * verify_stride for i in range(33)} | {last}
        # Verify a deterministic subset of reusable records, not hypothetical blocks.
        with gzip.open(history, "rt") as stream:
            next(stream)
            reuse_iter = (json.loads(line) for line in stream)
            previous = next(reuse_iter, None)
            pending = []
            paths = []
            semaphore = asyncio.Semaphore(args.workers)

            async def chunk(start, end, reused):
                path = cache / f"{start}-{end}.json.gz"
                receipt = path.with_suffix(".receipt.json")
                if path.exists() and receipt.exists():
                    record = json.loads(receipt.read_text())
                    if record["sha256"] != sha(path) or record["start"] != start or record["end"] != end:
                        raise ValueError("Cached collection chunk differs")
                    stats["cached_blocks"] += end - start + 1
                    return path
                async with semaphore:
                    audit = [
                        b
                        for b in reused
                        if b == first or b == last or any(b <= n < b + meta["block_step"] for n in verify)
                    ]
                    requested = [b for b in range(start, end + 1) if b not in reused or b in audit]
                    fresh = await fetch_rows(rpc, requested, meta["feed_first_available_block"]) if requested else []
                    for row in fresh:
                        old = reused.get(row["block"])
                        if old is not None and row != old:
                            raise ValueError(f"Reused observation differs at block {row['block']}")
                    joined = reused | {r["block"]: r for r in fresh}
                    if sorted(joined) != list(range(start, end + 1)):
                        raise ValueError("Incomplete collected chunk")
                    temporary = path.with_suffix(".partial")
                    with (
                        temporary.open("wb") as raw,
                        gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as dest,
                    ):
                        for b in range(start, end + 1):
                            validate_row(joined[b], meta["feed_first_available_block"])
                            dest.write((json.dumps(joined[b], sort_keys=True, separators=(",", ":")) + "\n").encode())
                    temporary.replace(path)
                    write(receipt, dict(start=start, end=end, sha256=sha(path)))
                    stats["reused_blocks"] += len(reused)
                    stats["new_blocks"] += end - start + 1 - len(reused)
                    stats["verified_reuse"] += len(audit)
                    return path

            async def finish():
                result = await asyncio.gather(*pending)
                paths.extend(result)
                stats["complete_blocks"] = min(stop - first + 1, len(paths) * 100)
                stats.update(rpc_calls=rpc.calls, retries=rpc.retries, elapsed_seconds=time.monotonic() - started)
                write(cache / "progress.json", stats)
                print(json.dumps(stats), flush=True)
                pending.clear()

            for start in range(first, stop + 1, 100):
                end = min(start + 99, stop)
                reused = {}
                while previous is not None and previous["block"] <= end:
                    if previous["block"] >= start:
                        reused[previous["block"]] = previous
                    previous = next(reuse_iter, None)
                pending.append(asyncio.create_task(chunk(start, end, reused)))
                if len(pending) >= args.workers:
                    await finish()
            if pending:
                await finish()
        if (await rpc.one("eth_getBlockByNumber", [hex(last), False]))["hash"] != pin["hash"]:
            raise ValueError("Chain anchor changed during collection")
        if args.pilot_blocks:
            stats.update(
                status="pilot_complete", cache=str(cache), raw_chunk_bytes=sum(p.stat().st_size for p in paths)
            )
            write(output, stats)
            return stats
        metadata = meta | dict(
            block_step=1,
            dense_ranges=[],
            record_count=last - first + 1,
            collection=identity,
            reused_history=dict(sha256=source_hash, pin=meta["pin"]),
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(".partial")
        count, previous_time = 0, -1
        with temporary.open("wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as dest:
            dest.write((json.dumps({"metadata": metadata}, sort_keys=True) + "\n").encode())
            for path in paths:
                with gzip.open(path, "rt") as stream:
                    for line in stream:
                        row = json.loads(line)
                        if row["block"] != first + count or row["timestamp"] <= previous_time:
                            raise ValueError("Nonconsecutive or nonchronological completed history")
                        validate_row(row, meta["feed_first_available_block"])
                        count += 1
                        previous_time = row["timestamp"]
                        dest.write(line.encode())
        if count != last - first + 1 or previous_time != meta["last_timestamp"]:
            raise ValueError("Incomplete final history")
        temporary.replace(output)
        stats.update(status="complete", sha256=sha(output), elapsed_seconds=time.monotonic() - started)
        write(output.with_suffix(".collection.json"), stats)
        return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", required=True)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--provider", choices=["configured", "alchemy", "infura"], default="configured")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--rate", type=int, default=80, help="Maximum RPC method requests per second")
    parser.add_argument("--pilot-blocks", type=int, default=0)
    args = parser.parse_args()
    if (
        not 1 <= args.workers <= 8
        or args.pilot_blocks < 0
        or not 1 <= args.batch_size <= 200
        or not 1 <= args.rate <= 5000
    ):
        parser.error("Invalid collection concurrency, batch size, rate or pilot size")
    try:
        asyncio.run(collect(args))
    except (Exception, KeyboardInterrupt) as exc:
        # Never print aiohttp exceptions or request bodies, which can contain keys.
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "detail": (
                        str(exc)
                        if isinstance(exc, (ValueError, RuntimeError, FileExistsError))
                        else "Collection stopped; validated cached chunks remain available"
                    ),
                }
            ),
            flush=True,
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()
