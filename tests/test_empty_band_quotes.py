import unittest

from simulator.amm.lending_amm import LendingAMM, OracleState, fee_multiplier
from simulator.amm.simulator import Simulator


class EmptyBandQuoteTest(unittest.TestCase):
    def replay(self, market, fee, memory):
        positions = []

        class InitialLiquidity:
            def __init__(self, price, bands):
                pass

            def deposit(self, amm, amount):
                amm.p_base = 1.01
                amm.min_band = amm.max_band = 1
                amm.active_band = 2
                amm.bands_x[1] = amount
                positions.append(amm)

        simulator = Simulator.__new__(Simulator)
        simulator.initial_liquidity_class = InitialLiquidity
        simulator.external_fee = 0.0
        simulator.log_enabled = simulator.verbose = False
        bars = [[0, market, market, market, market, 0]]
        simulator.calculate_loss(50, fee, bars, [1.0], 1, 0.0,
                                 initial_state=OracleState(1.0, memory, -30.0))
        return positions[0]

    def test_tiny_boundary_exchange_still_writes_oracle_memory(self):
        reference = LendingAMM(1.01, 50, 0.003, 0.0, oracle_state=OracleState(1.0, 0.2, -30.0))
        reference.min_band = reference.max_band = 1
        reference.active_band = 2
        reference.bands_x[1] = 1.0
        reference.set_p_oracle(1.0, timestamp=0.0)
        market = reference.p_down(2) / fee_multiplier(reference.dynamic_fee(1, timestamp=0.0))
        changes = reference.trade_to_price(market)
        amm = self.replay(market, 0.003, 0.2)
        # Rounding at a fee-adjusted boundary can leave a tiny nonzero exchange.
        # Suppressing it would leave the old timestamp and change later decay.
        self.assertLess(max(abs(value) for value in changes), 1e-12)
        self.assertEqual((amm.bands_x[1], amm.bands_y[1]), (reference.bands_x[1], reference.bands_y[1]))
        self.assertEqual(amm.active_band, reference.active_band)
        self.assertEqual(amm.oracle_state(), reference.oracle_state())

    def test_price_inside_empty_band_preserves_balances_and_memory(self):
        reference = LendingAMM(1.01, 50, 0.0, 0.0, oracle_state=OracleState(1.0, 0.0, -30.0))
        reference.active_band = 2
        market = (reference.get_p() + reference.p_down(2)) / 2
        amm = self.replay(market, 0.0, 0.0)
        self.assertEqual(amm.bands_x[1], 1.0)
        self.assertEqual(amm.bands_y[1], 0.0)
        self.assertEqual(amm.oracle_state(), OracleState(1.0, 0.0, -30.0))


if __name__ == "__main__":
    unittest.main()
