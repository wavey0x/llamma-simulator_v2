"""Deterministic minute OHLC and LP oracle preparation from complete block evidence.

The LP arithmetic is imported from the existing pinned reconstruction. Only the
recorded observation/update schedule changes. No RPC or app dependency is used.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import shutil
from pathlib import Path

import numpy as np

from simulator.import_data import reusd_sfrxusd_lp as lp

SCHEMA = "lp-causal-minute-candles-v1"
RECIPE = "lp-causal-candles-v1"
ORACLE_RULE = (
    "One modeled price_w per nonempty UTC minute using the latest completed block at or before the minute start"
)
TRACE_COLUMNS = [
    "timestamp",
    "block",
    "a_raw",
    "pool_oracle",
    "vp",
    "vp_ema",
    "ema_seconds",
    "lp_oracle",
    "bridge_oracle",
    "inverse_oracle",
    "redemption_fee",
    "floor",
    "feed",
    "capped",
    "lp_crvusd",
    "usd_rate",
    "usd_oracle",
    "pool_spot",
    "lp_spot",
    "bridge_spot",
    "inverse_spot",
    "spot",
]
COMPONENT_NAMES = [
    "timestamp",
    "block",
    "crvusd_usd",
    "lp_virtual_price",
    "lp_vp_ema",
    "reusd_feed",
    "bridge_spot",
    "bridge_oracle",
    "lp_spot_ratio",
    "lp_oracle_ratio",
    "source_timestamp",
    "observation_count",
    "open",
    "high",
    "low",
    "close",
    "closing_block",
    "closing_timestamp",
]


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(data)
    return h.hexdigest()


def dump(path, value):
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def spot_value(row):
    a = row["lp_A_precise"] * lp.A_PRECISION // 200
    value = lp.portfolio_value(a, row["lp_last_price"]) * row["lp_virtual_price"] // lp.WAD
    return value * (lp.WAD**2 // row["bridge_last_price"]) // lp.WAD


def market_float(value):
    # Match Curve's _get_price_at_block conversion (float before division).
    return float(value) / lp.WAD


def oracle_value(row, timestamp, ema):
    a = row["lp_A_precise"] * lp.A_PRECISION // 200
    smoothed = ema.price(row["lp_virtual_price"], timestamp, write=True)
    value = lp.portfolio_value(a, row["lp_price_oracle"]) * smoothed // lp.WAD
    inverse = lp.WAD**2 // row["bridge_price_oracle"]
    floor = lp.WAD - row["base_redemption_fee"]
    feed = max(inverse, floor) if row["reusd_feed"] is None else row["reusd_feed"]
    capped = lp.feed_value(row)
    in_crvusd = value * capped // lp.WAD
    oracle = in_crvusd * row["crvusd_agg_price"] // lp.WAD
    lp_spot = lp.portfolio_value(a, row["lp_last_price"]) * row["lp_virtual_price"] // lp.WAD
    inverse_spot = lp.WAD**2 // row["bridge_last_price"]
    spot = lp_spot * inverse_spot // lp.WAD
    return oracle, [
        timestamp,
        row["block"],
        a,
        row["lp_price_oracle"],
        row["lp_virtual_price"],
        smoothed,
        866,
        value,
        row["bridge_price_oracle"],
        inverse,
        row["base_redemption_fee"],
        floor,
        feed,
        capped,
        in_crvusd,
        row["crvusd_agg_price"],
        oracle,
        row["lp_last_price"],
        lp_spot,
        row["bridge_last_price"],
        inverse_spot,
        spot,
    ]


def validate_block(row, meta, ordinal, previous):
    if row["block"] != meta["first_block"] + ordinal or row["timestamp"] <= previous:
        raise ValueError("Uniform history has a missing, duplicate or unordered block")
    if len(row["block_hash"]) != 66 or int(row["block_hash"], 16) == 0:
        raise ValueError("Missing block hash")
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
            raise ValueError("Invalid required historical price")
    if (row["reusd_feed"] is None) != (row["block"] < meta["feed_first_available_block"]):
        raise ValueError("Missing deployed feed or unexpected predeployment value")
    lp.feed_value(row)


def minute_buckets(rows, first_timestamp):
    """Curve's UTC bucket boundaries; no synthetic prices for an empty bucket."""
    first_minute = (first_timestamp + 59) // 60 * 60
    current = previous = None
    for row in rows:
        timestamp = row["timestamp"] // 60 * 60
        if timestamp < first_minute:
            previous = row
            continue
        spot = spot_value(row)
        if current is not None and timestamp != current["timestamp"]:
            yield current
            current = None
        if current is None:
            current = dict(
                timestamp=timestamp,
                open=spot,
                high=spot,
                low=spot,
                close=spot,
                observation_count=0,
                row=row,
                oracle_row=row if row["timestamp"] == timestamp else previous,
            )
        current.update(
            high=max(current["high"], spot),
            low=min(current["low"], spot),
            close=spot,
            row=row,
            observation_count=current["observation_count"] + 1,
        )
        previous = row
    if current is not None:
        yield current


def prepare(history, output):
    history, output = Path(history), Path(output)
    output.mkdir(parents=True, exist_ok=True)
    if (output / "prices.jsonl.gz").exists():
        raise FileExistsError("Prepared candle evidence already exists")

    with gzip.open(history, "rt") as stream:
        meta = json.loads(next(stream))["metadata"]
        if (
            meta.get("schema") != 4
            or meta.get("chain_id") != 1
            or meta.get("block_step") != 1
            or meta.get("dense_ranges")
        ):
            raise ValueError("Minute preparation requires complete uniform block evidence")
        if meta["record_count"] != meta["last_block"] - meta["first_block"] + 1:
            raise ValueError("Uniform history count differs from its block range")
        capacity = math.ceil((meta["last_timestamp"] - meta["first_timestamp"]) / 60) + 1
        arrays = {
            name: np.lib.format.open_memmap(
                output / (name + ".building.npy"), mode="w+", dtype=dtype, shape=(capacity, width)
            )
            for name, width, dtype in [
                ("prices", 7, "<f8"),
                ("display-prices", 3, "<f8"),
                ("oracle-trace", 22, "<i8"),
                ("components", len(COMPONENT_NAMES), "<f8"),
                ("candle-evidence", 4, "<i8"),
            ]
        }
        source_times = np.lib.format.open_memmap(
            output / "source-times.npy", mode="w+", dtype="<i8", shape=(meta["record_count"],)
        )
        block_count, previous = 0, -1
        last = ema = None

        def checked_rows():
            nonlocal block_count, previous, last, ema
            for line in stream:
                row = json.loads(line)
                validate_block(row, meta, block_count, previous)
                if block_count == 0 and row["timestamp"] != meta["first_timestamp"]:
                    raise ValueError("First timestamp differs")
                if block_count == 0:
                    ema = lp.VirtualPriceEMA(row["lp_virtual_price"], row["timestamp"], 866)
                previous, last = row["timestamp"], row
                source_times[block_count] = previous
                block_count += 1
                yield row

        count, gaps, previous_minute, reconstructed = 0, [], None, 0
        excluded = [meta["first_timestamp"] // 60 * 60] if meta["first_timestamp"] % 60 else []
        records = output / "candle-records.partial.gz"
        with records.open("wb") as raw, gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as dest:
            for bar in minute_buckets(checked_rows(), meta["first_timestamp"]):
                t, row, closing = bar["timestamp"], bar["oracle_row"], bar["row"]
                if row is None:
                    excluded.append(t)
                    continue
                if row["timestamp"] > t:
                    raise ValueError("Oracle source is later than its modeled update")
                if previous_minute is not None and t - previous_minute != 60:
                    gaps.append([previous_minute + 60, t])
                oracle, trace = oracle_value(row, t, ema)
                arrays["prices"][count] = [
                    t,
                    *[market_float(bar[k]) for k in ("open", "high", "low", "close")],
                    0.0,
                    oracle / lp.WAD,
                ]
                arrays["display-prices"][count] = [t, market_float(bar["close"]), oracle / lp.WAD]
                arrays["oracle-trace"][count] = trace
                arrays["components"][count] = [
                    t,
                    row["block"],
                    *[
                        v / lp.WAD
                        for v in [
                            row["crvusd_agg_price"],
                            row["lp_virtual_price"],
                            trace[5],
                            trace[13],
                            row["bridge_last_price"],
                            row["bridge_price_oracle"],
                            row["lp_last_price"],
                            row["lp_price_oracle"],
                        ]
                    ],
                    row["timestamp"],
                    bar["observation_count"],
                    *[market_float(bar[k]) for k in ("open", "high", "low", "close")],
                    closing["block"],
                    closing["timestamp"],
                ]
                arrays["candle-evidence"][count] = [t, closing["block"], closing["timestamp"], bar["observation_count"]]
                record = {k: v for k, v in bar.items() if k not in ("row", "oracle_row")} | dict(
                    block=closing["block"],
                    source_timestamp=closing["timestamp"],
                    oracle_source_block=row["block"],
                    oracle_source_timestamp=row["timestamp"],
                    oracle=oracle,
                    volume=0,
                )
                dest.write((json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode())
                reconstructed += int(row["reusd_feed"] is None)
                count += 1
                previous_minute = t
        if (
            not count
            or block_count != meta["record_count"]
            or last["timestamp"] != meta["last_timestamp"]
            or last["block_hash"] != meta["pin"]["block_hash"]
        ):
            raise ValueError("Incomplete or differently anchored block history")
    first_timestamp = int(arrays["prices"][0, 0])
    source_times.flush()
    for name, array in arrays.items():
        array.flush()
        np.save(output / (name + ".npy"), array[:count])
        array._mmap.close()
        (output / (name + ".building.npy")).unlink()
    metadata = dict(
        schema=SCHEMA,
        recipe=RECIPE,
        record_count=count,
        raw_record_count=block_count,
        history=meta,
        history_sha256=sha(history),
        scale=lp.WAD,
        code_sha256={
            "reusd_sfrxusd_lp.py": sha(lp.__file__),
            "reusd_sfrxusd_lp_candles.py": sha(__file__),
        },
        config=dict(
            ema_seconds=866,
            oracle_updates=ORACLE_RULE,
            oracle_initialization="First historical virtual price at its actual source timestamp",
        ),
        units=dict(spot="crvUSD per LP", oracle="USD per LP", timestamp="UTC minute start"),
        candle_columns=["timestamp", "open", "high", "low", "close", "volume", "oracle"],
        first_timestamp=first_timestamp,
        last_timestamp=previous_minute,
        excluded_minutes=excluded,
        missing_minutes=gaps,
        partial_final_minute=meta["last_timestamp"] < previous_minute + 59,
        feed_reconstructed_rows=reconstructed,
        quote_convention="Preserve target deployment: USD/LP Oracle consumed numerically by crvUSD AMM; Spot is crvUSD/LP",
        target_source="https://github.com/wavey0x/curve-stablecoin/tree/958fea183badb831374f9c6e3a8cac7b808bf3b0",
    )
    with (
        (output / "prices.jsonl.gz").open("wb") as raw,
        gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as dest,
    ):
        dest.write((json.dumps({"metadata": metadata}, sort_keys=True) + "\n").encode())
        with gzip.open(records, "rb") as records_in:
            shutil.copyfileobj(records_in, dest)
    records.unlink()
    dump(
        output / "oracle-trace.json",
        dict(
            columns=TRACE_COLUMNS,
            dtype="little-endian int64 WAD",
            rows=count,
            sha256=sha(output / "oracle-trace.npy"),
            preparation_sha256=sha(__file__),
        ),
    )
    dump(output / "preparation.json", metadata)
    return metadata


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    metadata = prepare(args.history, args.output)
    print(json.dumps({key: metadata[key] for key in ("schema", "record_count", "raw_record_count", "history_sha256")}))


if __name__ == "__main__":
    main()
