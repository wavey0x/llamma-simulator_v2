import warnings
from array import array
from math import floor, fsum, log
from operator import index
from typing import NamedTuple

import cython

if cython.compiled:
    from cython.cimports.libc.math import isfinite
    from cython.cimports.libc.math import sqrt as _sqrt
else:
    from math import isfinite
    from math import sqrt as _sqrt


def sqrt(value):
    if value < 0:
        raise ValueError("math domain error")
    return _sqrt(value)


def initial_recovery_coefficient(A: int, bands: int) -> float:
    """Initial recovery / opening oracle for the unshifted synthetic position.

    The synthetic position uses bands 1 through N. The opening oracle lies just
    inside band 1; use its in-band recovery, then geometric recovery for 2..N.
    """
    q = (A - 1) / A
    upper = 1 + 1e-4 * q
    first = A * ((upper - 1) / upper**2 + (1 / upper - q) * sqrt(upper * q))
    return (first + upper * fsum(q ** (k + 0.5) for k in range(1, bands))) / bands


class OracleState(NamedTuple):
    """Persistent oracle memory written by the last exchange."""

    old_p_oracle: float
    old_dfee: float
    prev_p_oracle_time: float

    @classmethod
    def initial(cls, price: float, timestamp: float):
        return cls(price, 0.0, timestamp)


def fee_multiplier(fee: float) -> float:
    # The contract caps fees at 1 - 1e-18, which rounds to 1.0 as a float.
    return 1 / max(1 - fee, 1e-18)


def _power(value, exponent):
    # Avoid Cython's constant-power rewrites; final compiler rounding is checked separately.
    return value**exponent


# Cache only the dimensionless band factor. It depends on A and n, so changing
# the position's base price does not invalidate it or reuse a stale price.
_factor_A = 0.0
_factor_values = array("d", [0.0]) * 1002
_factor_valid = array("b", [0]) * 1002


@cython.boundscheck(False)
@cython.wraparound(False)
def _band_factor(A, n):
    global _factor_A
    if not -500 <= n <= 501:
        return _power((A - 1) / A, n)
    if A != _factor_A:
        for i in range(1002):
            _factor_valid[i] = 0
        _factor_A = A
    i = n + 500
    if not _factor_valid[i]:
        _factor_values[i] = _power((A - 1) / A, n)
        _factor_valid[i] = 1
    return _factor_values[i]


# Last-value caches are separate from persistent exchange memory.
_cube_price = 0.0
_cube_value = 0.0
_ratio_A = 0.0
_ratio_value = 0.0


def _cube(price):
    global _cube_price, _cube_value
    if price != _cube_price:
        _cube_value = _power(price, 3)
        _cube_price = price
    return _cube_value


def _ratio_square(A):
    global _ratio_A, _ratio_value
    if A != _ratio_A:
        _ratio_value = _power(A / (A - 1), 2)
        _ratio_A = A
    return _ratio_value


def _oracle_limit(price, old_price, old_dfee, dt, delay, min_ratio, max_change):
    limited_price = price
    ratio = 0.0

    if dt > 0 and old_price > 0:
        price_ratio = min(old_price, price) / max(old_price, price)
        if price > old_price and price_ratio < min_ratio:
            price_ratio = min_ratio
            limited_price = old_price * max_change
        elif price < old_price and price_ratio < min_ratio:
            price_ratio = min_ratio
            limited_price = old_price / max_change

        ratio = ((1.0 + old_dfee) - _power(price_ratio, 3)) * (dt / delay)
        # The on-chain cap of 1 - 1e-18 rounds to 1.0 as a float.
        ratio = min(max(ratio, 0.0), 1.0)

    return limited_price, ratio


class BandBalances:
    """Integer band balances with dense trading buffers and sparse overflow.

    Missing reads insert zero, as with defaultdict(float). Track represented
    bands separately so valuation preserves the original key set and order.
    """

    def __init__(self):
        self._values = array("d", [0.0]) * 1001
        self._present = array("b", [0]) * 1001
        self._keys = set()
        self._overflow = {}

    @cython.boundscheck(False)
    @cython.wraparound(False)
    def read(self, n):
        if -500 <= n <= 500:
            i = n + 500
            if not self._present[i]:
                self._keys.add(n)
                self._present[i] = 1
            return self._values[i]
        if n not in self._overflow:
            self._keys.add(n)
            self._overflow[n] = 0.0
        return self._overflow[n]

    @cython.boundscheck(False)
    @cython.wraparound(False)
    def write(self, n, value):
        if -500 <= n <= 500:
            i = n + 500
            if not self._present[i]:
                self._keys.add(n)
                self._present[i] = 1
            self._values[i] = value
        else:
            self._keys.add(n)
            self._overflow[n] = value

    def clear(self):
        for n in self._keys:
            if -500 <= n <= 500:
                self._values[n + 500] = 0.0
                self._present[n + 500] = 0
        self._keys.clear()
        self._overflow.clear()

    def __getitem__(self, n):
        return self.read(index(n))

    def __setitem__(self, n, value):
        self.write(index(n), value)

    def __delitem__(self, n):
        n = index(n)
        self._keys.remove(n)
        if -500 <= n <= 500:
            self._present[n + 500] = 0
            self._values[n + 500] = 0.0
        else:
            del self._overflow[n]

    def __iter__(self):
        return iter(self._keys)

    def __len__(self):
        return len(self._keys)

    def keys(self):
        return self._keys.copy()

    def values(self):
        return [self.read(n) for n in self._keys]

    def items(self):
        return [(n, self.read(n)) for n in self._keys]

    def update(self, values):
        for n, value in dict(values).items():
            self.write(index(n), value)

    def __eq__(self, other):
        return dict(self) == dict(other)

    def __deepcopy__(self, memo):
        result = BandBalances()
        result.update(self)
        return result


class LendingAMM:
    def __init__(
        self,
        p_base: float,
        A: int,
        fee: float,
        dynamic_fee_multiplier: float | None = None,
        *,
        oracle_state: OracleState | None = None,
    ):
        self.reset(p_base, A, fee, dynamic_fee_multiplier, oracle_state)

    def reset(self, p_base, A, fee, dynamic_fee_multiplier=None, oracle_state=None):
        """Clear the previous position and oracle memory before a new deposit."""
        self.PREV_P_O_DELAY = 2 * 60  # seconds
        self.MAX_P_O_CHANGE = 1.25  # matches on-chain MAX_P_O_CHG / 1e18
        self.MIN_PRICE_RATIO = 1 / self.MAX_P_O_CHANGE
        self.p_base = p_base
        self.p_oracle = p_base
        self.prev_p_oracle = p_base
        self.raw_p_oracle = p_base
        self.old_p_oracle = p_base
        self.old_dfee = 0.0
        self.prev_p_oracle_time: float | None = None
        self.current_timestamp: float | None = None
        if oracle_state is not None:
            self.restore_oracle_state(oracle_state)
        self.A = A
        self.dynamic_fee_multiplier = dynamic_fee_multiplier if dynamic_fee_multiplier is not None else 0.25
        if getattr(self, "bands_x", None) is None:
            self.bands_x = BandBalances()
            self.bands_y = BandBalances()
        else:
            self.bands_x.clear()
            self.bands_y.clear()
        self.min_band = self.max_band = self.active_band = 0
        self.fee = fee

    def oracle_state(self) -> OracleState:
        return OracleState(*(getattr(self, field) for field in OracleState._fields))

    def restore_oracle_state(self, state: OracleState):
        price = state.old_p_oracle
        fee = state.old_dfee
        timestamp = state.prev_p_oracle_time
        if not isfinite(price) or not isfinite(fee) or not isfinite(timestamp) or price <= 0 or not 0 <= fee <= 1:
            raise ValueError("Invalid oracle state")
        self.old_p_oracle = price
        self.old_dfee = fee
        self.prev_p_oracle_time = timestamp
        self.p_oracle = self.prev_p_oracle = self.raw_p_oracle = price
        self.current_timestamp = timestamp

    # Deposit:
    # - above active band - only in y,
    # - below active band - only in x
    # - transform band prices: p_band = p_oracle**3 / p_base**2
    # - add transformation to get_band, get_band_n, deposit_range
    # - add y0 ("invariant") changing with p_oracle
    # - get_price depending on current state (band, x[band], y[band])
    # - trade cross-bands: given dx or dy, calculate the destination band / move
    # - fees:
    #  - collect fees separately for the protocol (wohoo), compensate traded bands
    #  - reduced_input *= (1 - fee), calc output for reduced_input, split fee * input across bands touched

    def set_p_oracle(self, p, timestamp: float | None = None):
        """Observe the external oracle without committing exchange memory."""
        self._observe(p, timestamp)

    def _observe(self, p, timestamp=None):
        timestamp = self._normalize_timestamp(timestamp)
        if (
            not isfinite(p)
            or p <= 0
            or (timestamp is not None and not isfinite(timestamp))
            or (self.current_timestamp is not None and timestamp < self.current_timestamp)
        ):
            raise ValueError("Oracle observations require positive prices and nondecreasing finite timestamps")
        self.raw_p_oracle = p
        self.current_timestamp = timestamp
        self.prev_p_oracle = self.p_oracle
        snapshot = self._price_oracle_view(timestamp)
        self.p_oracle, _ = snapshot
        return snapshot

    def dynamic_fee(self, n_band, timestamp: float | None = None):
        """Replicates on-chain logic: max(base fee, oracle-memory fee, distance fee)."""
        p_oracle, oracle_memory_fee = self._price_oracle_view(timestamp)
        return self._dynamic_fee(n_band, p_oracle, oracle_memory_fee)

    def _dynamic_fee(self, n_band, p_oracle, oracle_memory_fee):
        fee_with_memory = max(self.fee, oracle_memory_fee)
        distance_fee = self._distance_fee(p_oracle, n_band)
        return max(fee_with_memory, distance_fee)

    def _normalize_timestamp(self, timestamp: float | None) -> float | None:
        if timestamp is None:
            timestamp = self.current_timestamp
        if timestamp is None:
            warnings.warn("Timestamp not provided. Oracle memory decay disabled; fee memory pinned at max.")
        return timestamp

    def _memory_dt(self, timestamp: float | None) -> float:
        if self.prev_p_oracle_time is None or timestamp is None:
            return self.PREV_P_O_DELAY
        current = timestamp
        previous = self.prev_p_oracle_time
        delay = self.PREV_P_O_DELAY
        elapsed = max(current - previous, 0)
        return delay - min(delay, elapsed)

    def _limit_price_oracle(self, price: float, timestamp: float | None) -> tuple[float, float]:
        timestamp = self._normalize_timestamp(timestamp)
        return _oracle_limit(
            price,
            self.old_p_oracle,
            self.old_dfee,
            self._memory_dt(timestamp),
            self.PREV_P_O_DELAY,
            self.MIN_PRICE_RATIO,
            self.MAX_P_O_CHANGE,
        )

    def _price_oracle_view(self, timestamp: float | None) -> tuple[float, float]:
        price = self.raw_p_oracle if self.raw_p_oracle is not None else self.p_oracle
        return self._limit_price_oracle(price, timestamp)

    def _commit_oracle(self, snapshot: tuple[float, float]):
        self.old_p_oracle, self.old_dfee = snapshot
        self.prev_p_oracle_time = self.current_timestamp
        # The next view can differ from the snapshot used by this exchange.
        self.p_oracle, _ = self._price_oracle_view(self.current_timestamp)

    def exchange_zero(self):
        """An explicit zero-input exchange still writes oracle memory on-chain."""
        self._commit_oracle(self._price_oracle_view(self.current_timestamp))
        return 0, 0

    def _distance_fee(self, p_oracle: float, n_band: int) -> float:
        p_o_up = self.p_top(n_band)
        if p_o_up <= 0:
            return 0.0

        # Matches on-chain: p_c_d = p_o**3 / p_o_up**2, p_c_u = p_c_d * (A / (A-1))**2
        p_c_d = _cube(p_oracle) / _power(p_o_up, 2)
        p_c_u = p_c_d * _ratio_square(self.A)

        if p_oracle < p_c_d and p_c_d > 0:
            return (p_c_d - p_oracle) / p_c_d * self.dynamic_fee_multiplier
        if p_oracle > p_c_u and p_oracle > 0:
            return (p_oracle - p_c_u) / p_oracle * self.dynamic_fee_multiplier
        return 0.0

    def p_down(self, n_band, p_oracle: float | None = None):
        """
        Lower price for the band at the current p_oracle
        """
        if p_oracle is None:
            price = self.p_oracle
        else:
            price = p_oracle
        return _cube(price) / _power(self.p_top(n_band), 2)

    def p_up(self, n_band, p_oracle: float | None = None):
        """
        Upper price for the band at the current p_oracle
        """
        if p_oracle is None:
            price = self.p_oracle
        else:
            price = p_oracle
        return _cube(price) / _power(self.p_top(n_band + 1), 2)

    def p_top(self, n):
        # Prices which show start and end of band when p_oracle = p
        return self.p_base * _band_factor(self.A, n)

    def p_bottom(self, n):
        k = (self.A - 1) / self.A  # equal to (p_down / p_up)
        return self.p_top(n) * k

    def get_band_n(self, p):
        """
        Rounds correct way for both higher and lower prices
        """
        k = (self.A - 1) / self.A  # equal to (p_down / p_up)
        return floor(log(p / self.p_base) / log(k))

    def deposit_range(self, amount, p1, p2):
        assert p1 <= self.p_oracle and p2 <= self.p_oracle
        n1 = self.get_band_n(p1)
        n2 = self.get_band_n(p2)
        n1, n2 = sorted([n1, n2])
        y = amount / (n2 - n1 + 1)
        self.min_band = min(n1, n2)
        self.max_band = max(n1, n2)
        for i in range(n1, n2 + 1):
            assert self.bands_x.read(i) == 0
            self.bands_y.write(i, self.bands_y.read(i) + y)

    def deposit_nrange(self, amount, p, dn):
        n_top = self.get_band_n(self.p_oracle) + 1
        assert p <= self.p_oracle
        n1 = max(self.get_band_n(p), n_top)
        n2 = n1 + dn - 1
        y = amount / dn
        self.min_band = n1
        self.max_band = n2
        for i in range(n1, n2 + 1):
            assert self.bands_x.read(i) == 0
            self.bands_y.write(i, self.bands_y.read(i) + y)

    def get_y0(self, n=None):
        A = self.A
        if n is None:
            band = self.active_band
        else:
            band = n
        x = self.bands_x.read(band)
        y = self.bands_y.read(band)
        p_o = self.p_oracle
        p_top = self.p_top(band)

        # solve:
        # p_o * A * y0**2 - y0 * (p_top/p_o * (A-1) * x + p_o**2/p_top * A * y) - xy = 0
        a = p_o * A
        b = p_top / p_o * (A - 1) * x + _power(p_o, 2) / p_top * A * y
        D = _power(b, 2) + 4 * a * x * y
        return (b + sqrt(D)) / (2 * a)

    def get_f(self, y0=None, n=None):
        if y0 is None:
            value = self.get_y0()
        else:
            value = y0
        if n is None:
            band = self.active_band
        else:
            band = n
        return self._get_f(value, band)

    def _get_f(self, value, band):
        p_top = self.p_top(band)
        p_oracle = self.p_oracle
        return value * _power(p_oracle, 2) / p_top * self.A

    def get_g(self, y0=None, n=None):
        if y0 is None:
            value = self.get_y0()
        else:
            value = y0
        if n is None:
            band = self.active_band
        else:
            band = n
        return self._get_g(value, band)

    def _get_g(self, value, band):
        p_top = self.p_top(band)
        p_oracle = self.p_oracle
        return value * p_top / p_oracle * (self.A - 1)

    def get_p(self, y0=None):
        x = self.bands_x.read(self.active_band)
        y = self.bands_y.read(self.active_band)
        if x == 0 and y == 0:
            return _power(self.p_up(self.active_band) * self.p_down(self.active_band), 0.5)
        else:
            if y0 is None:
                value = self.get_y0()
            else:
                value = y0
            return (self._get_f(value, self.active_band) + x) / (self._get_g(value, self.active_band) + y)

    def trade_to_price(self, price) -> tuple:
        """
        Not the method to be present in real smart contract, for simulations only
        Returns tuple of x and y changes in target band

        price is the external market price after external execution costs only.
        This method applies the AMM fee per band; callers must not pre-apply it.
        """

        snapshot = self._price_oracle_view(self.current_timestamp)
        self.p_oracle, oracle_memory_fee = snapshot
        original_band = self.active_band

        if self.bands_x.read(self.active_band) == 0 and self.bands_y.read(self.active_band) == 0:
            # If current band is empty - steps are determined by whether current price is higher or lower than
            # boundaries
            if price > self.p_up(self.active_band):
                bstep = 1
            elif price < self.p_down(self.active_band):
                bstep = -1
            else:
                return 0, 0

        else:
            current_price = self.get_p()
            if price > current_price:
                bstep = 1  # going up: sell
            elif price < current_price:
                bstep = -1  # going down: buy
            else:
                return 0, 0

        dx = 0
        dy = 0

        original_price = price

        while True:
            n = self.active_band
            assert -500 < n < 500, "active band should not exceed 500"

            x = self.bands_x.read(n)
            y = self.bands_y.read(n)

            if x == 0 and y == 0:
                if self.p_down(n) <= price <= self.p_up(n):
                    break
                self.active_band += bstep
                continue

            y0 = self.get_y0()
            g = self._get_g(y0, n)
            f = self._get_f(y0, n)
            # (f + x)(g + y) = const = p_oracle * A**2 * y0**2 = I
            Inv = (f + x) * (g + y)
            # p = (f + x) / (g + y) => p * (g + y)**2 = I or (f + x)**2 / p = I
            price = original_price

            fee = max(self.fee, oracle_memory_fee, self._distance_fee(self.p_oracle, n))
            antifee = fee_multiplier(fee)

            if bstep == 1:  # up
                price = price / antifee
                if price <= (f + x) / (g + y):
                    break

                # reduce y, increase x, go up
                y_dest = _power(Inv / price, 0.5) - g
                x_old = self.bands_x.read(n)
                if y_dest >= 0:
                    # End the cycle
                    self.bands_y.write(n, y_dest)
                    self.bands_x.write(n, Inv / (g + y_dest) - f)
                    delta_x = self.bands_x.read(n) - x_old
                    self.bands_x.write(n, x_old + delta_x * antifee)
                    dx += self.bands_x.read(n) - x
                    dy += self.bands_y.read(n) - y
                    break

                else:
                    self.bands_y.write(n, 0)
                    self.bands_x.write(n, Inv / g - f)
                    delta_x = self.bands_x.read(n) - x_old
                    self.bands_x.write(n, x_old + delta_x * antifee)
                    self.active_band += 1

            else:  # down
                price = price * antifee
                if price >= (f + x) / (g + y):
                    break

                # increase y, reduce x, go down
                x_dest = _power(Inv * price, 0.5) - f
                y_old = self.bands_y.read(n)
                if x_dest >= 0:
                    # End the cycle
                    self.bands_x.write(n, x_dest)
                    self.bands_y.write(n, Inv / (f + x_dest) - g)
                    delta_y = self.bands_y.read(n) - y_old
                    self.bands_y.write(n, y_old + delta_y * antifee)
                    dx += self.bands_x.read(n) - x
                    dy += self.bands_y.read(n) - y
                    break

                else:
                    self.bands_x.write(n, 0)
                    self.bands_y.write(n, Inv / f - g)
                    delta_y = self.bands_y.read(n) - y_old
                    self.bands_y.write(n, y_old + delta_y * antifee)
                    self.active_band -= 1

            dx += self.bands_x.read(n) - x
            dy += self.bands_y.read(n) - y

        if dx or dy:
            # Commit once after all bands have used the captured snapshot.
            self._commit_oracle(snapshot)
        else:
            self.active_band = original_band
        return dx, dy

    def get_y_up(self, n):
        """
        Measure the amount of y in the band n if we adiabatically trade near p_oracle on the way up
        """
        x = self.bands_x.read(n)
        y = self.bands_y.read(n)
        if x == 0 and y == 0:
            return 0
        p_o = self.p_oracle
        p_o_up = self.p_top(n)
        p_o_down = p_o_up * (self.A - 1) / self.A
        p_current_mid = _power(p_o, 3) / _power(p_o_down, 2) * (self.A - 1) / self.A
        sqrt_band_ratio = sqrt(self.A / (self.A - 1))

        if x == 0 or y == 0:
            if p_o > p_o_up:
                # all to y at constant p_o, then to target currency adiabatically
                y_equiv = y
                if y == 0:
                    y_equiv = x / p_current_mid
                return y_equiv

            elif p_o < p_o_down:
                x_equiv = x
                if x == 0:
                    x_equiv = y * p_current_mid
                return x_equiv * sqrt_band_ratio / p_o_up

        y0 = self.get_y0(n)
        g = self._get_g(y0, n)
        f = self._get_f(y0, n)
        # (f + x)(g + y) = const = p_top * A**2 * y0**2 = I
        Inv = (f + x) * (g + y)
        # p = (f + x) / (g + y) => p * (g + y)**2 = I or (f + x)**2 / p = I

        # First, "trade" in this band to p_oracle
        # x_o = 0
        # y_o = 0

        if p_o > p_o_up:  # p_o < p_current_down, all to y
            # x_o = 0
            y_o = max(Inv / f, g) - g
            return y_o

        elif p_o < p_o_down:  # p_o > p_current_up, all to x
            # y_o = 0
            x_o = max(Inv / g, f) - f
            return x_o * sqrt_band_ratio / p_o_up

        else:
            # y_o__ = max(sqrt(Inv / p_o), g) - g
            # x_o__ = max(Inv / (g + y_o__), f) - f

            y_o = self.A * y0 * (1 - p_o_down / p_o)
            x_o = max(Inv / (g + y_o), f) - f

            # Now adiabatic conversion from definitely in-band
            return y_o + x_o / sqrt(p_o_up * p_o)

    def get_x_down(self, n):
        """
        Measure the amount of x in the band n if we adiabatically trade near p_oracle on the way up
        """
        x = self.bands_x.read(n)
        y = self.bands_y.read(n)
        if x == 0 and y == 0:
            return 0
        p_o = self.p_oracle
        p_o_up = self.p_top(n)
        p_o_down = p_o_up * (self.A - 1) / self.A
        p_current_mid = _power(p_o, 3) / _power(p_o_down, 2) * (self.A - 1) / self.A
        sqrt_band_ratio = sqrt(self.A / (self.A - 1))

        if x == 0 or y == 0:
            if p_o > p_o_up:
                # all to y at constant p_o, then to target currency adiabatically
                y_equiv = y
                if y == 0:
                    y_equiv = x / p_current_mid
                return y_equiv * p_o_up / sqrt_band_ratio

            elif p_o < p_o_down:
                x_equiv = x
                if x == 0:
                    x_equiv = y * p_current_mid
                return x_equiv

        y0 = self.get_y0(n)
        g = self._get_g(y0, n)
        f = self._get_f(y0, n)
        # (f + x)(g + y) = const = p_top * A**2 * y0**2 = I
        Inv = (f + x) * (g + y)
        # p = (f + x) / (g + y) => p * (g + y)**2 = I or (f + x)**2 / p = I

        # First, "trade" in this band to p_oracle
        # x_o = 0
        # y_o = 0

        if p_o > p_o_up:  # p_o < p_current_down, all to y
            # x_o = 0
            y_o = max(Inv / f, g) - g
            return y_o * p_o_up / sqrt_band_ratio

        elif p_o < p_o_down:  # p_o > p_current_up, all to x
            # y_o = 0
            x_o = max(Inv / g, f) - f
            return x_o

        else:
            # y_o = max(sqrt(Inv / p_o), g) - g
            # x_o = max(Inv / (g + y_o), f) - f

            y_o = self.A * y0 * (1 - p_o_down / p_o)
            x_o = max(Inv / (g + y_o), f) - f
            # Now adiabatic conversion from definitely in-band
            return x_o + y_o * sqrt(p_o_down * p_o)

    # Include bands in either balance map, even outside the deposited range.
    # Keep legacy bounds and ascending summation; do not populate unrepresented bands.
    def get_all_y(self):
        total = 0.0
        for i in sorted(self.bands_x._keys | self.bands_y._keys):
            if -500 <= i < 500:
                total += self.get_y_up(i)
        return total

    def get_all_x(self):
        total = 0.0
        for i in sorted(self.bands_x._keys | self.bands_y._keys):
            if -500 <= i < 500:
                total += self.get_x_down(i)
        return total


def find_target_price(amm, p, p_oracle, memory_fee, is_up=True):
    # Find target band
    if is_up:
        for n in range(amm.max_band, amm.min_band - 1, -1):
            p_down = amm.p_down(n)
            target = p / fee_multiplier(amm._dynamic_fee(n, p_oracle, memory_fee))

            if target > p_down:
                return target

    else:
        for n in range(amm.min_band, amm.max_band + 1):
            p_up = amm.p_up(n)
            target = p * fee_multiplier(amm._dynamic_fee(n, p_oracle, memory_fee))

            if target < p_up:
                return target

    # price is outside of liquidity
    if is_up:
        return p / fee_multiplier(amm._dynamic_fee(amm.min_band, p_oracle, memory_fee))
    else:
        return p * fee_multiplier(amm._dynamic_fee(amm.max_band, p_oracle, memory_fee))
