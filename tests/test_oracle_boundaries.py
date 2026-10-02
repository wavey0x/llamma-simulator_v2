import unittest
import warnings

import numpy as np

from simulator.amm.intitial_liquidity import ConstantInitialLiquidity
from simulator.amm.lending_amm import LendingAMM, OracleState
from simulator.amm.simulator import Simulator, replay_batch


def observed_state(amm):
    return (amm.raw_p_oracle, amm.p_oracle, amm.prev_p_oracle,
            amm.current_timestamp, amm.oracle_state())


class OracleBoundaryTest(unittest.TestCase):
    def test_invalid_observations_leave_state_unchanged(self):
        for initial in (None, OracleState.initial(1.0, 60.0)):
            amm = LendingAMM(1.0, 100, 0.003, oracle_state=initial)
            for observe in (amm.set_p_oracle, amm._observe):
                for timestamp in (-float("inf"), float("inf"), float("nan")):
                    before = observed_state(amm)
                    with self.assertRaises(ValueError):
                        observe(1.1, timestamp)
                    self.assertEqual(observed_state(amm), before)
            if initial is not None:
                before = observed_state(amm)
                with self.assertRaises(ValueError):
                    amm.set_p_oracle(1.1, 30.0)
                self.assertEqual(observed_state(amm), before)

    def test_missing_timestamp_preserves_optional_clock_behavior(self):
        amm = LendingAMM(1.0, 100, 0.003)
        with self.assertWarns(UserWarning):
            amm.set_p_oracle(1.1)
        self.assertIsNone(amm.current_timestamp)
        self.assertIsNone(amm.prev_p_oracle_time)
        amm.set_p_oracle(1.2, 60.0)
        before = amm.oracle_state()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            amm.set_p_oracle(1.3, None)
        self.assertEqual(caught, [])
        self.assertEqual(amm.current_timestamp, 60.0)
        self.assertEqual(amm.oracle_state(), before)
        self.assertEqual(amm._price_oracle_view(None), amm._price_oracle_view(60.0))

    def test_optional_properties_and_raw_price_fallback(self):
        amm = LendingAMM(1.0, 100, 0.003, oracle_state=OracleState.initial(1.0, 0.0))
        for field in ("current_timestamp", "prev_p_oracle_time"):
            setattr(amm, field, None)
            self.assertIsNone(getattr(amm, field))
            setattr(amm, field, 0.0)
            for value in (-float("inf"), float("inf"), float("nan")):
                with self.assertRaises(ValueError):
                    setattr(amm, field, value)
                self.assertEqual(getattr(amm, field), 0.0)
        amm.raw_p_oracle = None
        for price in (1.1, 1.2):
            amm.p_oracle = price
            self.assertEqual(amm._price_oracle_view(180.0)[0], price)
            self.assertIsNone(amm.raw_p_oracle)
        amm.set_p_oracle(1.05, 180.0)
        self.assertEqual(amm.raw_p_oracle, 1.05)

    def test_batch_and_single_replay_reject_invalid_observation_times(self):
        sim = Simulator.__new__(Simulator)
        sim.initial_liquidity_class = ConstantInitialLiquidity
        sim.log_enabled = sim.verbose = False
        sim.external_fee = 0.0005
        records = np.array([[100, 0.003, 0, 2, 4, 0.0005, 0.25, 0]], dtype=float)
        for timestamp in (-float("inf"), float("inf"), float("nan"), 30.0):
            points = np.array([[60, 1, 1.1, 0.9, 1, 0, 1],
                               [timestamp, 1, 1.1, 0.9, 1, 0, 1]], dtype=float)
            with self.assertRaises(ValueError):
                replay_batch(sim, points, records)
            with self.assertRaises(ValueError):
                sim.calculate_loss(100, 0.003, points[:, :6], points[:, 6], 4, 0.25)
            # Unused rows are not observations in this replay.
            one_row = records.copy()
            one_row[0, 3] = 1
            self.assertTrue(np.isfinite(replay_batch(sim, points, one_row)).all())


if __name__ == "__main__":
    unittest.main()
