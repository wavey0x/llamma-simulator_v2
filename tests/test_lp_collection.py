"""Offline collection assembly checks; no archive endpoint is contacted."""

import argparse
import asyncio
import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_lp_candles import fixture

from simulator.import_data import reusd_sfrxusd_lp_blocks as collector


class CollectionTests(unittest.TestCase):
    def test_concurrency_cache_reuse_and_reordered_completion_preserve_exact_history(self):
        rows = fixture(333)
        indexed = {r["block"]: r for r in rows}
        sparse = rows[::5] + ([rows[-1]] if rows[-1] not in rows[::5] else [])
        meta = dict(
            schema=4,
            chain_id=1,
            first_block=rows[0]["block"],
            last_block=rows[-1]["block"],
            block_step=5,
            dense_ranges=[],
            record_count=len(sparse),
            first_timestamp=rows[0]["timestamp"],
            last_timestamp=rows[-1]["timestamp"],
            feed_first_available_block=10,
            pin={"block_hash": rows[-1]["block_hash"]},
        )

        class RPC:
            def __init__(self, *args):
                self.calls = 0
                self.retries = 0

            async def one(self, method, params):
                self.calls += 1
                if method == "eth_chainId":
                    return "0x1"
                assert method == "eth_getBlockByNumber"
                return {"hash": rows[-1]["block_hash"]}

        called = []

        async def fetch(rpc, blocks, feed_start):
            await asyncio.sleep((333 - blocks[0] % 333) / 100000)
            called.extend(blocks)
            return [indexed[b] for b in blocks]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history = root / "sparse.gz"
            source_hash = collector.source.write_history(history, meta, sparse)
            hashes = []
            with (
                patch.object(collector, "RPC", RPC),
                patch.object(collector, "fetch_rows", fetch),
                patch.object(collector, "endpoint", lambda _: "unused"),
            ):
                for label, workers, cache in (("one", 1, "one"), ("many", 4, "many"), ("cached", 4, "one")):
                    before = len(called)
                    args = argparse.Namespace(
                        history=str(history),
                        source_sha256=source_hash,
                        cache=str(root / cache),
                        output=str(root / (label + ".gz")),
                        provider="configured",
                        workers=workers,
                        batch_size=20,
                        rate=80,
                        pilot_blocks=0,
                    )
                    stats = asyncio.run(collector.collect(args))
                    self.assertEqual(stats["status"], "complete")
                    self.assertEqual(stats["complete_blocks"], len(rows))
                    if label == "cached":
                        self.assertEqual(len(called), before)
                    hashes.append(collector.sha(root / (label + ".gz")))
                    with gzip.open(root / (label + ".gz"), "rt") as stream:
                        header = json.loads(next(stream))["metadata"]
                        self.assertEqual(header["block_step"], 1)
                        self.assertEqual([json.loads(line) for line in stream], rows)
            self.assertEqual(len(set(hashes)), 1)


if __name__ == "__main__":
    unittest.main()
