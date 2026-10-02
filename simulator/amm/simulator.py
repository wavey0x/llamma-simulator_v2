import logging
import random
from datetime import datetime
from math import isfinite
from multiprocessing import Pool

import numpy as np
import psutil

from .intitial_liquidity import BaseRangeInitialLiquidity
from .lending_amm import LendingAMM, OracleState, fee_multiplier, find_target_price
from .price_history_loader import BasePriceHistoryLoader, VolatilityPriceHistoryLoader
from .price_oracle import BasePriceOracle

logger = logging.getLogger(__name__)


def _calculate_loss(
    simulator,
    A: int,
    fee: float,
    prices_for_simulation,
    oracle_prices_for_simulation,
    initial_liquidity_range: int,  # p0 then n number of bands
    dynamic_fee_multiplier: float | None = None,
    position_shift: float = 0,  # [0, 1) how much lower from current prices
    initial_state: OracleState | None = None,
    workspace=None,
    external_fee_override=None,
):
    """Replay one position from numeric candles and aligned oracle observations."""
    if len(prices_for_simulation) == 0 or len(prices_for_simulation) != len(oracle_prices_for_simulation):
        raise ValueError("Replay requires nonempty, aligned candles and oracle observations")
    timestamp = float(prices_for_simulation[0, 0])
    oracle_price = float(oracle_prices_for_simulation[0])
    if initial_state is None:
        # Synthetic windows start with zero memory; observations do not
        # reveal the exchanges needed to reconstruct earlier AMM memory.
        initial_state = OracleState.initial(oracle_price, timestamp)
    if initial_state.prev_p_oracle_time > timestamp:
        raise ValueError("Starting oracle state is later than the first candle")
    if workspace is None:
        amm = LendingAMM(oracle_price, A, fee, dynamic_fee_multiplier, oracle_state=initial_state)
    else:
        amm = workspace
        amm.reset(oracle_price, A, fee, dynamic_fee_multiplier, initial_state)
    snapshot = amm._observe(oracle_price, timestamp=timestamp)
    p0 = amm.p_oracle * (1 - position_shift)

    initial_y0 = 1.0  # 1 ETH
    amm.p_base = p0 * (A / (A - 1) + 1e-4)
    initial_x_value = initial_y0 * amm.p_base

    # Fill ticks with liquidity
    simulator.initial_liquidity_class(p0, initial_liquidity_range).deposit(amm, initial_y0)
    initial_all_x = amm.get_all_x()
    if not isfinite(initial_all_x) or initial_all_x <= 0:
        raise ValueError("Initial recovery value must be finite and positive")

    xs_normalized = []
    external_fee = simulator.external_fee if external_fee_override is None else external_fee_override
    log_enabled = simulator.log_enabled
    verbose = simulator.verbose

    # <----------------- Calculation ----------------->
    for i in range(len(prices_for_simulation)):
        t = float(prices_for_simulation[i, 0])
        high = float(prices_for_simulation[i, 2])
        low = float(prices_for_simulation[i, 3])
        oracle_price = float(oracle_prices_for_simulation[i])
        if i:
            snapshot = amm._observe(oracle_price, timestamp=t)

        high_external = high * (1 - external_fee)
        low_external = low * (1 + external_fee)
        # Use fee-adjusted targets only to check profitability. The AMM
        # applies its own per-band fee inside trade_to_price().
        # Distance fees can only increase the base/oracle fee. Skip the band
        # search when even that minimum fee makes this direction unprofitable.
        current_price = amm.get_p()
        antifee = fee_multiplier(max(amm.fee, snapshot[1]))
        if high_external / antifee > current_price:
            high = find_target_price(amm, high_external, snapshot[0], snapshot[1], is_up=True)
            if high > current_price:
                amm.trade_to_price(high_external)
                snapshot = amm._price_oracle_view(t)
                antifee = fee_multiplier(max(amm.fee, snapshot[1]))
                current_price = amm.get_p()

        # Not correct for dynamic fees which are too high
        # if high > max_price:
        #     # Check that AMM has only stablecoins
        #     for n in range(amm.min_band, amm.max_band + 1):
        #         assert amm.bands_y[n] == 0
        #         assert amm.bands_x[n] > 0

        if low_external * antifee < current_price:
            low = find_target_price(amm, low_external, snapshot[0], snapshot[1], is_up=False)
            if low < current_price:
                amm.trade_to_price(low_external)

        # Not correct for dynamic fees which are too high
        # if low < min_price:
        #     # Check that AMM has only collateral
        #     for n in range(amm.min_band, amm.max_band + 1):
        #         assert amm.bands_x[n] == 0
        #         assert amm.bands_y[n] > 0

        if log_enabled:
            d = datetime.fromtimestamp(t).strftime("%Y/%m/%d %H:%M")
            current_x_total_normalized = amm.get_all_x() / initial_x_value
            logger.info(
                f"Current x total for {d}: {current_x_total_normalized:.4f}, oracle price: {oracle_price:.2f}, amm_price: {amm.get_p():.2f}"
            )

        if verbose:
            current_x_total_normalized = amm.get_all_x() / initial_x_value
            xs_normalized.append([t, current_x_total_normalized])

    if verbose:
        logger.info(f"Xs after trades list: {xs_normalized}")

    loss = 1 - amm.get_all_x() / initial_all_x
    if not isfinite(loss):
        raise ValueError("Final loss is not finite")
    return loss


class Simulator:

    def __init__(
        self,
        initial_liquidity_class: type[BaseRangeInitialLiquidity],
        price_history_loader: BasePriceHistoryLoader,
        price_oracle: BasePriceOracle,
        external_fee: float = 0.0,  # should be 0 < external_fee < 1
    ):
        """
        :param initial_liquidity_class: initial liquidity for AMM (class, initialized in simulator), min=4 worst case
        :param price_history_loader: load prices source
        :param price_oracle: oracle prices calculater (can choose different oracles)
        :param external_fee: fee paid by arbitragers to external platforms

        min_loan_duration: minimal duration of loan in liquidation in days (actual is chosen randomly every run)
        max_loan_duration: maximum duration of loan in liquidation days (actual is chosen randomly every run)
        log_enabled: enable logging
        verbose: Output losses after each iteration for every run

        Usually positions are in liquidation in < 30 min so 1/48 is reasonable approximation
        """

        self.initial_liquidity_class = initial_liquidity_class
        self.price_history_loader = price_history_loader
        self.price_oracle = price_oracle
        self.external_fee = external_fee

        # Default parameters
        self.samples = 400
        self.min_loan_duration = 1 / 48  # days
        self.max_loan_duration = 1 / 24  # days
        self.log_enabled: bool = False
        self.verbose: bool = False

        self.prices = self.load_prices()
        self.oracle_prices = self.calculate_oracle_price(self.prices)
        if len(self.prices) != len(self.oracle_prices):
            raise ValueError("Candle and oracle observations do not align")
        first = next((i for i, value in enumerate(self.oracle_prices) if value is not None), len(self.prices))
        self.prices = self.prices[first:]
        self.oracle_prices = self.oracle_prices[first:]
        if not self.prices or any(value is None for value in self.oracle_prices):
            raise ValueError("No complete causal oracle history for replay")
        previous_timestamp = float("-inf")
        for row, price in zip(self.prices, self.oracle_prices):
            timestamp = row[0]
            if not isfinite(price) or price <= 0 or not isfinite(timestamp) or timestamp <= previous_timestamp:
                raise ValueError("Oracle history must have positive prices and increasing finite timestamps")
            previous_timestamp = timestamp

    def load_prices(self) -> list:
        return self.price_history_loader.load_prices()

    def calculate_oracle_price(self, prices: list) -> list:
        return self.price_oracle.calculate_oracle_prices(prices)

    def single_run(
        self,
        A: int,
        fee: float,
        position_start: float,  # [0, 1)
        position_period: float,  # [0, 1 - position_start)
        initial_liquidity_range: int,  # p0 then n number of bands
        dynamic_fee_multiplier: float | None = None,
        position_shift: float = 0,  # [0, 1) how much lower from current prices
    ):
        """
        position: 0..1
        size: fraction of all price data length for size
        """
        # Data for prices
        position_start_index = int(position_start * len(self.prices))  # start of position in prices array
        position_end_index = int(
            (position_start + position_period) * len(self.prices)
        )  # end of position in prices array

        prices_for_simulation = self.prices[position_start_index:position_end_index]
        oracle_prices_for_simulation = self.oracle_prices[position_start_index:position_end_index]

        return self.calculate_loss(
            A,
            fee,
            prices_for_simulation,
            oracle_prices_for_simulation,
            initial_liquidity_range,
            dynamic_fee_multiplier,
            position_shift,
        )

    def calculate_loss(
        self,
        A: int,
        fee: float,
        prices_for_simulation: list,
        oracle_prices_for_simulation: list,
        initial_liquidity_range: int,  # p0 then n number of bands
        dynamic_fee_multiplier: float | None = None,
        position_shift: float = 0,  # [0, 1) how much lower from current prices
        *,
        initial_state: OracleState | None = None,
    ):
        if len(prices_for_simulation) == 0 or len(prices_for_simulation) != len(oracle_prices_for_simulation):
            raise ValueError("Replay requires nonempty, aligned candles and oracle observations")
        candles = np.asarray(prices_for_simulation, dtype=np.float64)
        oracles = np.asarray(oracle_prices_for_simulation, dtype=np.float64)
        if candles.ndim != 2 or candles.shape[1] != 6 or oracles.ndim != 1:
            raise ValueError("Replay requires six candle columns and one oracle column")
        return _calculate_loss(
            self,
            A,
            fee,
            candles,
            oracles,
            initial_liquidity_range,
            dynamic_fee_multiplier,
            position_shift,
            initial_state=initial_state,
        )

    def single_run_kw(self, kw):
        return self.single_run(**kw)


class SimulatorV2(Simulator):
    def __init__(
        self,
        initial_liquidity_class: type[BaseRangeInitialLiquidity],
        price_history_loader: VolatilityPriceHistoryLoader,
        price_oracle: BasePriceOracle,
        external_fee: float = 0.0,  # should be 0 < external_fee < 1
    ):
        """
        Simulator for volatility adjusted price loader
        :param initial_liquidity_class: initial liquidity for AMM (class, initialized in simulator), min=4 worst case
        :param price_history_loader: load prices source
        :param price_oracle: oracle prices calculater (can choose different oracles)
        :param external_fee: fee paid by arbitragers to external platforms
        """
        super().__init__(initial_liquidity_class, price_history_loader, price_oracle, external_fee)

    def single_run_v2(
        self,
        A: int,
        fee: float,
        position_start: float,  # [0, 1)
        position_period: float,  # [0, 1 - position_start)
        initial_liquidity_range: int,  # p0 then n number of bands
        dynamic_fee_multiplier: float | None = None,
        position_shift: float = 0,  # [0, 1) how much lower from current prices
    ):
        """
        position: 0..1
        size: fraction of all price data length for size
        """
        # Data for prices
        position_start_index = int(position_start * len(self.prices))  # start of position in prices array
        position_end_index = int(
            (position_start + position_period) * len(self.prices)
        )  # end of position in prices array

        prices_for_simulation = self.prices[position_start_index:position_end_index]
        is_down, prices_for_simulation = self.price_history_loader.change_period(prices_for_simulation)
        oracle_prices_for_simulation = self.oracle_prices[position_start_index:position_end_index]

        if not is_down:
            return 0

        return self.calculate_loss(
            A,
            fee,
            prices_for_simulation,
            oracle_prices_for_simulation,
            initial_liquidity_range,
            dynamic_fee_multiplier,
            position_shift,
        )

    def single_run_v2_kw(self, kw):
        return self.single_run_v2(**kw)


def get_loss_rate(
    initial_liquidity_class: type[BaseRangeInitialLiquidity],
    price_history_loader: BasePriceHistoryLoader,
    price_oracle: BasePriceOracle,
    external_fee: float,
    A: int,
    fee: float,
    initial_liquidity_range: int,
    dynamic_fee_multiplier: float | None = None,
    samples: int | None = None,
    n_top_samples: int | None = None,
    max_loan_duration: float | None = None,
    min_loan_duration: float | None = None,
    position_shift: float = 0,
    use_threading: bool = True,
):
    simulator = Simulator(
        initial_liquidity_class=initial_liquidity_class,
        price_history_loader=price_history_loader,
        price_oracle=price_oracle,
        external_fee=external_fee,
    )

    if not samples:
        samples = simulator.samples
    if not max_loan_duration:
        max_loan_duration = simulator.max_loan_duration
    if not min_loan_duration:
        min_loan_duration = simulator.min_loan_duration

    day_fraction = 86400 / (simulator.prices[-1][0] - simulator.prices[0][0])  # Which fraction of all data is 1 day

    kwargs_list = []
    for _ in range(samples):
        position_start = random.random()
        position_period = min_loan_duration * day_fraction
        position_period += (max_loan_duration - min_loan_duration) * day_fraction * random.random()

        kwargs_list.append(
            {
                "A": A,
                "fee": fee,
                "position_start": position_start,
                "position_period": position_period,
                "initial_liquidity_range": initial_liquidity_range,
                "dynamic_fee_multiplier": dynamic_fee_multiplier,
                "position_shift": position_shift,
            }
        )

    if use_threading:
        pool = Pool(psutil.cpu_count(logical=False))
        results = pool.map(simulator.single_run_kw, kwargs_list)
    else:
        results = []
        for kw in kwargs_list:
            sr_result = simulator.single_run(**kw)
            if simulator.log_enabled:
                logger.info(f"Results A:{kw['A']}, position_start:{kw['position_start']}: {sr_result}")
            results.append(sr_result)

    if not n_top_samples:
        n_top_samples = samples // 20
    return sum(sorted(results)[::-1][:n_top_samples]) / n_top_samples


def get_loss_rate_v2(
    initial_liquidity_class: type[BaseRangeInitialLiquidity],
    price_history_loader: VolatilityPriceHistoryLoader,
    price_oracle: BasePriceOracle,
    external_fee: float,
    A: int,
    fee: float,
    initial_liquidity_range: int,
    dynamic_fee_multiplier: float | None = None,
    samples: int | None = None,
    n_top_samples: int | None = None,
    max_loan_duration: float | None = None,
    min_loan_duration: float | None = None,
    position_shift: float = 0,
    use_threading: bool = True,
):
    simulator = SimulatorV2(
        initial_liquidity_class=initial_liquidity_class,
        price_history_loader=price_history_loader,
        price_oracle=price_oracle,
        external_fee=external_fee,
    )

    if not samples:
        samples = simulator.samples
    if not max_loan_duration:
        max_loan_duration = simulator.max_loan_duration
    if not min_loan_duration:
        min_loan_duration = simulator.min_loan_duration

    day_fraction = 86400 / (simulator.prices[-1][0] - simulator.prices[0][0])  # Which fraction of all data is 1 day

    kwargs_list = []
    for _ in range(samples):
        position_start = random.random()
        position_period = min_loan_duration * day_fraction
        position_period += (max_loan_duration - min_loan_duration) * day_fraction * random.random()

        kwargs_list.append(
            {
                "A": A,
                "fee": fee,
                "position_start": position_start,
                "position_period": position_period,
                "initial_liquidity_range": initial_liquidity_range,
                "dynamic_fee_multiplier": dynamic_fee_multiplier,
                "position_shift": position_shift,
            }
        )

    if use_threading:
        pool = Pool(psutil.cpu_count(logical=False))
        results = pool.map(simulator.single_run_v2_kw, kwargs_list)
    else:
        results = []
        for kw in kwargs_list:
            sr_result = simulator.single_run_v2(**kw)
            if simulator.log_enabled:
                logger.info(f"Results A:{kw['A']}, position_start:{kw['position_start']}: {sr_result}")
            results.append(sr_result)

    if not n_top_samples:
        results = [r for r in results if r > 0]
        n_top_samples = len(results) // 20

    return sum(sorted(results)[::-1][:n_top_samples]) / n_top_samples


def replay_batch(simulator, points, records):
    """Replay resolved windows without changing the simulator's settings.

    Both inputs are two-dimensional float64 arrays. Points contain timestamp,
    open, high, low, close, volume and oracle price. Records contain A, fee,
    start, end, bands, external fee, dynamic-fee multiplier and an unused slot.
    Start/end are resolved integer indices; end is exclusive. Positions are
    unshifted and begin with default oracle memory. Storage is reset per window.
    """
    if points.ndim != 2 or records.ndim != 2 or points.shape[1] != 7 or records.shape[1] != 8:
        raise ValueError("Expected seven price columns and eight task columns")
    values = np.empty(len(records), dtype=np.float64)
    output = values  # The native profile exposes this array as a typed view.
    workspace = LendingAMM(1.0, 2, 0.0)
    for i in range(len(records)):
        lo = int(records[i, 2])
        hi = int(records[i, 3])
        output[i] = _calculate_loss(
            simulator,
            float(records[i, 0]),
            float(records[i, 1]),
            points[lo:hi, :6],
            points[lo:hi, 6],
            int(records[i, 4]),
            float(records[i, 6]),
            0.0,
            None,
            workspace,
            float(records[i, 5]),
        )
    return values
