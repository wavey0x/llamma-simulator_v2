import unittest
from math import sqrt
from unittest.mock import Mock, patch

from simulator.amm.intitial_liquidity import ConstantInitialLiquidity
from simulator.amm.lending_amm import LendingAMM
from simulator.amm.simulator import Simulator


def make_simulator(candles, oracle_prices, external_fee, liquidity=ConstantInitialLiquidity):
    loader = Mock()
    loader.load_prices.return_value = candles
    oracle = Mock()
    oracle.calculate_oracle_prices.return_value = oracle_prices
    return Simulator(liquidity, loader, oracle, external_fee)


def replay_position(amm, candle, oracle_price, external_fee):
    # Keep the prepared balances rather than depositing a new position.
    simulator = make_simulator([candle], [oracle_price], external_fee, liquidity=Mock())
    with patch("simulator.amm.simulator.LendingAMM", return_value=amm):
        simulator.calculate_loss(amm.A, amm.fee, [candle], [oracle_price], 4)


class AmmFeeTargetTest(unittest.TestCase):
    def test_execution_receives_market_price_with_only_external_cost(self):
        candles = [
            [0, 100.0, 100.0, 95.0, 95.0, 1.0],
            [180, 95.0, 105.0, 95.0, 105.0, 1.0],
        ]
        oracle_prices = [100.0, 101.0]
        candles_by_time = {candle[0]: candle for candle in candles}
        execute = LendingAMM.trade_to_price

        for fee, multiplier in ((0.002, 0.0), (0.017, 0.25)):
            for external_fee in (0.0, 0.0005):
                with self.subTest(fee=fee, multiplier=multiplier, external_fee=external_fee):
                    directions = set()

                    def checked_trade(amm, price):
                        candle = candles_by_time[amm.current_timestamp]
                        is_up = price > amm.get_p()
                        market = candle[2] if is_up else candle[3]
                        expected = market * (1 - external_fee if is_up else 1 + external_fee)
                        self.assertAlmostEqual(price, expected)
                        dx, dy = execute(amm, price)
                        if dx or dy:
                            directions.add(is_up)
                            self.assertGreaterEqual(dx if is_up else -dx, 0)
                            self.assertGreaterEqual(-dy if is_up else dy, 0)
                        return dx, dy

                    simulator = make_simulator(candles, oracle_prices, external_fee)
                    with patch.object(LendingAMM, "trade_to_price", autospec=True, side_effect=checked_trade):
                        simulator.calculate_loss(210, fee, candles, oracle_prices, 4, multiplier)

                    self.assertEqual(directions, {False, True})

    def test_single_band_balances_match_one_fee_adjustment(self):
        # Independent constant-product solution, including retained input fees.
        for is_up in (False, True):
            for memory_fee in (0.0, 0.02):
                with self.subTest(is_up=is_up, memory_fee=memory_fee):
                    external_fee = 0.0005
                    amm = LendingAMM(1.0, 50, 0.003, 0.0)
                    amm.min_band = amm.max_band = amm.active_band = 0
                    amm.bands_x[0] = amm.bands_y[0] = 1.0
                    amm.set_p_oracle(1.0, timestamp=0)
                    amm.old_dfee = memory_fee

                    # With an unchanged oracle, its fee memory halves at 60s.
                    fee = max(amm.fee, memory_fee / 2)
                    current = amm.get_p()
                    boundary = amm.p_up(0) if is_up else amm.p_down(0)
                    target = (current + boundary) / 2
                    external = target / (1 - fee if is_up else 1 + fee)
                    market = external / (1 - external_fee if is_up else 1 + external_fee)

                    f, g = amm.get_f(), amm.get_g()
                    invariant = (f + 1.0) * (g + 1.0)
                    expected_x = sqrt(invariant * target) - f
                    expected_y = sqrt(invariant / target) - g
                    if is_up:
                        expected_x += fee * (expected_x - 1.0)
                    else:
                        expected_y += fee * (expected_y - 1.0)

                    replay_position(amm, [60, market, market, market, market, 1.0], 1.0, external_fee)

                    self.assertEqual(amm.active_band, 0)
                    self.assertAlmostEqual(amm.bands_x[0], expected_x, places=11)
                    self.assertAlmostEqual(amm.bands_y[0], expected_y, places=11)
                    self.assertAlmostEqual(amm.dynamic_fee(0, timestamp=60), fee)

    def test_market_inside_fee_spread_does_not_trade(self):
        amm = LendingAMM(1.0, 50, 0.017, 0.0)
        amm.min_band = amm.max_band = amm.active_band = 0
        amm.bands_x[0] = amm.bands_y[0] = 1.0
        amm.set_p_oracle(1.0, timestamp=0)
        market = amm.get_p()

        with patch.object(amm, "trade_to_price", wraps=amm.trade_to_price) as trade:
            replay_position(amm, [180, market, market, market, market, 1.0], 1.0, 0.0005)

        trade.assert_not_called()
        self.assertEqual(amm.bands_x[0], 1.0)
        self.assertEqual(amm.bands_y[0], 1.0)


if __name__ == "__main__":
    unittest.main()
