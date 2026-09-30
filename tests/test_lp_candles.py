"""Offline candle preparation checks; run with python -m unittest discover -s tests -p test_lp_candles.py."""

import asyncio
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from simulator.import_data import reusd_sfrxusd_lp as lp
from simulator.import_data import reusd_sfrxusd_lp_candles as candles
from simulator.import_data.onchain import base


def fixture(count=1000):
    wad = 10**18
    rows = [
        dict(
            timestamp=1742480772 + i * 12,
            block=10 + i,
            block_hash="0x" + f"{i+1:064x}",
            lp_A_precise=40000,
            lp_virtual_price=wad + int(np.sin(i / 31) * 0.003 * wad),
            lp_last_price=wad + int(np.sin(i / 13) * 0.04 * wad),
            lp_price_oracle=wad,
            bridge_last_price=wad + int(np.sin(i / 11) * 0.04 * wad),
            bridge_price_oracle=wad,
            base_redemption_fee=wad // 100,
            reusd_feed=wad,
            crvusd_agg_price=wad,
        )
        for i in range(count)
    ]
    return rows


def history(path, rows):
    meta = dict(
        schema=4,
        chain_id=1,
        first_block=rows[0]["block"],
        last_block=rows[-1]["block"],
        block_step=1,
        dense_ranges=[],
        first_timestamp=rows[0]["timestamp"],
        last_timestamp=rows[-1]["timestamp"],
        feed_first_available_block=10,
        record_count=len(rows),
        pin={"block_hash": rows[-1]["block_hash"]},
    )
    return lp.write_history(path, meta, rows)


class CandleTests(unittest.TestCase):
    def test_buckets_match_unmodified_curve_importer(self):
        rows = fixture(53)
        # A genuinely empty minute and a partially observed final minute.
        for row in rows[20:]:
            row["timestamp"] += 120

        class Eth:
            async def get_block(self, number):
                return rows[number - 10] if number - 10 < len(rows) else {"timestamp": rows[-1]["timestamp"] + 12}

        class Web3:
            eth = Eth()

            def __init__(self, *args):
                pass

        class Price:
            async def call(self, block_identifier):
                return candles.spot_value(rows[block_identifier - 10])

        class Importer(base.OnchainImporter):
            anchor_block = 10
            start = dt.datetime.fromtimestamp(rows[0]["timestamp"], dt.timezone.utc)
            end = dt.datetime.fromtimestamp(rows[-1]["timestamp"] + 1, dt.timezone.utc)

        with patch.object(base, "AsyncWeb3", Web3):
            expected = asyncio.run(Importer.get_klines(Price(), 18))
        actual = [
            [b["timestamp"], *[candles.market_float(b[k]) for k in ("open", "high", "low", "close")], 0.0, 0.0]
            for b in candles.minute_buckets(rows, rows[0]["timestamp"])
        ]
        np.testing.assert_array_equal(actual, expected)

    def test_preparation_is_repeatable_and_rejects_incomplete_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = fixture(103)
            for row in rows[20:]:
                row["timestamp"] += 120
            history(root / "history.gz", rows)
            a = candles.prepare(root / "history.gz", root / "one")
            b = candles.prepare(root / "history.gz", root / "two")
            self.assertEqual(a, b)
            self.assertTrue(a["missing_minutes"])
            for name in ("prices.npy", "prices.jsonl.gz", "source-times.npy", "oracle-trace.npy", "components.npy"):
                self.assertEqual(candles.sha(root / "one" / name), candles.sha(root / "two" / name))
            for name, mutate in (
                ("missing", lambda r: r.pop(8)),
                ("duplicate", lambda r: r.insert(8, r[7].copy())),
                ("unordered", lambda r: r.__setitem__(8, r[9].copy())),
            ):
                bad = [r.copy() for r in rows]
                mutate(bad)
                history(root / (name + ".gz"), bad)
                with self.assertRaises(ValueError):
                    candles.prepare(root / (name + ".gz"), root / name)

    def test_opening_inputs_use_latest_completed_block_including_boundary_blocks(self):
        rows = fixture(53)
        for row in rows[20:]:
            row["timestamp"] += 120
        bars = list(candles.minute_buckets(rows, rows[0]["timestamp"]))
        for bar in bars:
            expected = max((r for r in rows if r["timestamp"] <= bar["timestamp"]), key=lambda r: r["timestamp"])
            self.assertEqual(bar["oracle_row"], expected)
            self.assertGreaterEqual(bar["row"]["timestamp"], bar["timestamp"])
        self.assertTrue(any(b["oracle_row"]["timestamp"] == b["timestamp"] for b in bars))
        self.assertTrue(any(b["oracle_row"]["timestamp"] < b["timestamp"] for b in bars))

    def test_future_changes_cannot_change_opening_oracle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = fixture(103)
            history(root / "history.gz", rows)
            candles.prepare(root / "history.gz", root / "original")
            original = np.load(root / "original/prices.npy")
            cutoff = original[6, 0]
            for row in rows:
                if row["timestamp"] > cutoff:
                    row["lp_virtual_price"] += 10**16
                    row["lp_price_oracle"] += 10**16
                    row["crvusd_agg_price"] += 10**16
            history(root / "future.gz", rows)
            candles.prepare(root / "future.gz", root / "changed")
            changed = np.load(root / "changed/prices.npy")
            np.testing.assert_array_equal(original[:7, 6], changed[:7, 6])
            self.assertFalse(np.array_equal(original[7:, 6], changed[7:, 6]))
            # The current candle can have different later OHLC observations.
            self.assertFalse(np.array_equal(original[6, 1:5], changed[6, 1:5]))

    def test_smoothing_starts_at_first_historical_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = fixture(53)
            rows[0]["lp_virtual_price"] -= 10**17
            history(root / "history.gz", rows)
            header = candles.prepare(root / "history.gz", root / "prepared")
            trace = np.load(root / "prepared/oracle-trace.npy")
            ema = lp.VirtualPriceEMA(rows[0]["lp_virtual_price"], rows[0]["timestamp"], 866)
            bar = next(candles.minute_buckets(rows, rows[0]["timestamp"]))
            expected = ema.price(bar["oracle_row"]["lp_virtual_price"], bar["timestamp"], write=True)
            self.assertEqual(int(trace[0, 5]), expected)
            self.assertNotEqual(expected, bar["oracle_row"]["lp_virtual_price"])
            self.assertEqual(header["excluded_minutes"], [rows[0]["timestamp"] // 60 * 60])
