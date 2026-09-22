"""Focused replay checks; optional compiled-contract parity uses REUSD_ORACLE_SOURCE.

The optional check needs titanoboa and the dependencies from the pinned
curve-stablecoin checkout. Normal tests need only the simulator's locked environment.
"""

import hashlib
import importlib.metadata
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from simulator.pairs.reusd_sfrxusd_lp.calculate import (
    WAD,
    Config,
    VirtualPriceEMA,
    contract_valid,
    feed_value,
    portfolio_value,
    reconstruct,
    run,
    simulate,
    window_starts,
)
from simulator.pairs.reusd_sfrxusd_lp.collect_history import read_history, sample_blocks, write_history


def observation(timestamp=0, block=1, **changes):
    return dict(
        timestamp=timestamp,
        block=block,
        lp_A_precise=40000,
        lp_virtual_price=WAD,
        lp_last_price=WAD,
        lp_price_oracle=WAD,
        bridge_last_price=WAD,
        bridge_price_oracle=WAD,
        base_redemption_fee=WAD // 100,
        reusd_feed=None,
        crvusd_agg_price=WAD,
        **changes,
    )


class ReplayTests(unittest.TestCase):
    def test_deployment_fee_limit_depends_on_A(self):
        self.assertTrue(contract_valid(1000, 0.004))
        self.assertFalse(contract_valid(1000, 0.004001))
        self.assertFalse(contract_valid(10001, 0.0001))
        self.assertFalse(contract_valid(100, 0))
        with self.assertRaisesRegex(ValueError, "deployment bounds"):
            run([(0, 1, 1.0, 1.0)], Config(a_values=(1000,), fees=(0.01,)), 1, starts=[0], exact=True)

    def test_joint_search_refines_A_at_each_fee(self):
        config = Config(a_values=(50, 100, 200), fees=(0.002, 0.004))
        points = [(0, 1, 1.0, 1.0)]

        def score(mapper, points, starts, A, fee, config):
            target = 100 if fee == 0.002 else 150
            return {"A": A, "fee": fee, "band_adjusted_loss": (A - target) ** 2 / 10000 + fee}

        with patch("simulator.pairs.reusd_sfrxusd_lp.calculate.evaluate", side_effect=score), patch("builtins.print"):
            result = run(points, config, 1, starts=[0])
        evaluated = {(r["A"], r["fee"]) for r in result["evaluations"]}
        self.assertTrue({(a, f) for a in config.a_values for f in config.fees}.issubset(evaluated))
        self.assertEqual([(r["A"], r["fee"]) for r in result["best_by_fee"]], [(100, 0.002), (150, 0.004)])

    def test_floor_cap_and_observed_feed(self):
        row = observation()
        row["bridge_price_oracle"] = WAD * 100 // 98
        self.assertEqual(feed_value(row), WAD * 99 // 100)
        row["bridge_price_oracle"] = WAD * 99 // 100
        self.assertEqual(feed_value(row), WAD)
        row["reusd_feed"] = WAD
        with self.assertRaises(ValueError):
            feed_value(row)

    def test_units_and_independent_spot(self):
        row = observation()
        row["bridge_price_oracle"] = row["bridge_last_price"] = WAD * 100 // 98
        _, _, spot, oracle = reconstruct([row], Config())[0]
        self.assertAlmostEqual(spot, 0.98, places=8)
        self.assertAlmostEqual(oracle, 0.99, places=8)

    def test_usd_aggregator_changes_only_the_supplied_oracle(self):
        row = observation()
        row["bridge_price_oracle"] = row["bridge_last_price"] = WAD * 100 // 98
        for aggregator in (90 * WAD // 100, 110 * WAD // 100):
            row["crvusd_agg_price"] = aggregator
            _, _, spot, oracle = reconstruct([row], Config())[0]
            self.assertAlmostEqual(spot, 0.98, places=8)
            self.assertAlmostEqual(oracle, 0.99 * aggregator / WAD, places=8)
        row["crvusd_agg_price"] = 0
        with self.assertRaisesRegex(ValueError, "USD aggregator"):
            reconstruct([row], Config())
        del row["crvusd_agg_price"]
        with self.assertRaises(KeyError):
            reconstruct([row], Config())

    def test_ema_queued_upside_downside_and_read_only(self):
        ema = VirtualPriceEMA(WAD, 0, 866)
        self.assertEqual(ema.price(2 * WAD, 12), WAD)
        same_block = ema.price(2 * WAD, 12)
        self.assertEqual(same_block, WAD)
        state = (ema.previous, ema.queued, ema.timestamp)
        self.assertGreater(ema.price(2 * WAD, 878, write=False), WAD)
        self.assertEqual((ema.previous, ema.queued, ema.timestamp), state)
        self.assertEqual(ema.price(WAD // 2, 900), WAD // 2)
        self.assertEqual(ema.price(WAD, 1800), WAD // 2)

    def test_no_lookahead_or_ema_reset_between_windows(self):
        rows = [observation(i * 12, i + 1) for i in range(100)]
        for row in rows[40:]:
            row["lp_virtual_price"] = 2 * WAD
        whole = reconstruct(rows, Config())
        self.assertEqual(whole[:50], reconstruct(rows[:50], Config()))
        self.assertLess(whole[50][3], reconstruct(rows[50:], Config())[0][3])
        with self.assertRaises(ValueError):
            reconstruct(rows[::-1], Config())

    def test_sampling_union_and_invalid_ranges(self):
        self.assertEqual(sample_blocks(10, 30, 5, [(17, 22)]), [10, 15, 17, 18, 19, 20, 21, 22, 25, 30])
        with self.assertRaises(ValueError):
            sample_blocks(10, 30, 5, [(9, 11)])

    def test_missing_samples_and_deployed_feed_fail_closed(self):
        rows = [observation(i * 60, 10 + i * 5) for i in range(3)]
        for row in rows:
            row["block_hash"] = "0x" + "ab" * 32
        rows[-1]["reusd_feed"] = WAD
        metadata = dict(
            schema=4,
            chain_id=1,
            first_block=10,
            last_block=20,
            block_step=5,
            first_timestamp=0,
            last_timestamp=120,
            feed_first_available_block=20,
            pin={"block_hash": rows[-1]["block_hash"]},
            record_count=3,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.gz"
            write_history(path, metadata, rows)
            self.assertEqual(read_history(path)[1], rows)
            rows[0]["crvusd_agg_price"] = 0
            write_history(path, metadata, rows)
            with self.assertRaisesRegex(ValueError, "USD aggregator"):
                read_history(path)
            rows[0]["crvusd_agg_price"] = WAD
            rows[-1]["reusd_feed"] = None
            write_history(path, metadata, rows)
            with self.assertRaisesRegex(ValueError, "missing deployed feed"):
                read_history(path)
            metadata["record_count"] = 2
            write_history(path, metadata, [rows[0], rows[-1]])
            with self.assertRaisesRegex(ValueError, "missing or duplicate sampled block"):
                read_history(path)

    def test_windows_reproducible_and_workers_agree(self):
        points = [(i * 600, i + 1, 1 - 0.01 * (i % 12) / 12, 1.0) for i in range(100)]
        config = Config(a_values=(150,), fees=(0.002,), warmup_seconds=600, loan_seconds=3600, window_step_seconds=3600)
        starts = window_starts(points, config)
        self.assertEqual(starts, window_starts(points, config))
        self.assertEqual(run(points, config, 1, starts, exact=True), run(points, config, 2, starts, exact=True))
        self.assertEqual(simulate([(0, 1, 1.0, 1.0), (86400, 2, 1.0, 1.0)], 150, 0.002, config), 0.0)

    def test_launch_day_cannot_seed_calibration_windows(self):
        # A large launch dislocation must not consume a stress-window slot or
        # introduce a pre-warm-up loan start. The raw point remains available.
        points = [(i * 3600, i + 1, 0.5 if i == 1 else 1.0, 1.0) for i in range(217)]
        starts = window_starts(points, Config())
        quiet_launch = [(t, b, 1.0, oracle) for t, b, _, oracle in points]
        self.assertEqual(starts, window_starts(quiet_launch, Config()))
        self.assertEqual(points[starts[0]][0], 86400)
        self.assertTrue(all(points[i][0] >= 86400 for i in starts))
        self.assertEqual(points[1][2], 0.5)


@unittest.skipUnless(os.environ.get("REUSD_ORACLE_SOURCE"), "optional pinned Vyper contract check")
class ContractParityTests(unittest.TestCase):
    def test_compiled_three_leg_chain(self):
        import boa

        source = Path(os.environ["REUSD_ORACLE_SOURCE"])
        chain_source = source.with_name("ChainOracle.vy")
        self.assertEqual(
            hashlib.sha256(chain_source.read_bytes()).hexdigest(),
            "7d5beafc61cd63249ba24ab871cbcafba9a33192f6e447e5e83afd9e41de45fe",
        )
        mock = """
# pragma version 0.4.3
p: uint256
@external
def set_price(answer: uint256):
    self.p = answer
@external
@view
def price() -> uint256:
    return self.p
@external
def price_w() -> uint256:
    return self.p
"""
        legs = [boa.loads(mock) for _ in range(3)]
        for leg in legs:
            leg.set_price(WAD)
        chain = boa.load(str(chain_source), [leg.address for leg in legs])
        for lp_price in (98 * WAD // 100, WAD, 110 * WAD // 100):
            for bridge in (98 * WAD // 100, WAD, 105 * WAD // 100):
                for agg in (90 * WAD // 100, WAD, 110 * WAD // 100):
                    row = observation()
                    row.update(lp_price_oracle=lp_price, bridge_price_oracle=bridge, crvusd_agg_price=agg)
                    lp = portfolio_value(row["lp_A_precise"] * 10000 // 200, lp_price)
                    for leg, price in zip(legs, (lp, feed_value(row), agg)):
                        leg.set_price(price)
                    expected = chain.price()
                    self.assertEqual(chain.price_w(), expected)
                    self.assertEqual(reconstruct([row], Config())[0][3], expected / WAD)

    def test_compiled_contract(self):
        import boa
        import curve_std

        source = Path(os.environ["REUSD_ORACLE_SOURCE"])
        self.assertEqual(
            hashlib.sha256(source.read_bytes()).hexdigest(),
            "f47bda3c1415f74c88d157da47dc2fabe8d2ad9769523f3434bb495ec6c6c975",
        )
        for package, revision in [
            ("curve-std", "048cb23d0ed4c48768815ea5c46d4a676da8de35"),
            ("stableswap-ng", "7a9f6f11fb67e4778fb2496e65a09ec2939e339c"),
        ]:
            installed = json.loads(importlib.metadata.distribution(package).read_text("direct_url.json"))
            self.assertEqual(installed["vcs_info"]["commit_id"], revision)
        math_contract = boa.load(str(Path(next(iter(curve_std.__path__))) / "stableswap/lp_oracle_2.vy"))
        for a in [1, 10000, 1000000, 2000000, 1000000000]:
            for price in [10**16, 98 * WAD // 100, WAD, 11 * WAD // 10, 10**20]:
                self.assertEqual(portfolio_value(a, price), math_contract.portfolio_value(a, price))

        pool = boa.loads(
            """
# pragma version 0.4.3
A_precise: public(uint256)
get_virtual_price: public(uint256)
p: uint256
@external
def set_state(a: uint256, vp: uint256, price: uint256):
    self.A_precise = a
    self.get_virtual_price = vp
    self.p = price
@external
@view
def price_oracle(i: uint256) -> uint256:
    assert i == 0
    return self.p
@external
@view
def coins(i: uint256) -> address:
    assert i < 2
    return empty(address)
"""
        )
        history = Path(__file__).resolve().parents[1] / "data/REUSD_SFRXUSD_LP/history.jsonl.gz"
        _, rows = read_history(history)
        # Uniform historical samples plus both price-dislocation neighborhoods.
        samples = rows[:: max(1, len(rows) // 1000)]
        samples += [r for r in rows if abs(r["block"] - 24361675) <= 200 or abs(r["block"] - 25762255) <= 200]
        samples = sorted({r["block"]: r for r in samples}.values(), key=lambda r: r["block"])
        max_error, observations = 0, 0
        for interval in [0, 300, 3600]:
            with boa.env.anchor():
                first = samples[0]
                pool.set_state(first["lp_A_precise"], first["lp_virtual_price"], first["lp_price_oracle"])
                contract = boa.load(str(source), pool.address, 0, 866)
                origin = boa.env.evm.patch.timestamp
                ema = VirtualPriceEMA(first["lp_virtual_price"], origin, 866)
                sequence = [
                    (
                        r["timestamp"] - first["timestamp"],
                        r["lp_A_precise"],
                        r["lp_virtual_price"],
                        r["lp_price_oracle"],
                    )
                    for r in samples
                ]
                # Consecutive upward, read-only, same-block, and downward transitions.
                end = sequence[-1][0]
                sequence += [
                    (end + dt, 40000, vp, WAD)
                    for dt, vp in [
                        (12, 2 * WAD),
                        (12, 3 * WAD),
                        (24, WAD // 2),
                        (36, 2 * WAD),
                        (4000, 2 * WAD),
                        (8000, WAD // 2),
                    ]
                ]
                for elapsed, a, vp, price in sequence:
                    timestamp = origin + elapsed
                    boa.env.evm.patch.timestamp = timestamp
                    pool.set_state(a, vp, price)
                    write = timestamp - ema.timestamp >= interval
                    expected = portfolio_value(a * 10000 // 200, price) * ema.price(vp, timestamp, write) // WAD
                    observed = contract.price_w() if write else contract.price()
                    error = abs(expected - observed)
                    max_error = max(max_error, error)
                    observations += 1
                    self.assertLessEqual(error, 1000)
        print(
            json.dumps({"contract_parity_observations": observations, "maximum_absolute_error_wei": max_error}),
            flush=True,
        )


if __name__ == "__main__":
    unittest.main()
