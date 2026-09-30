from collections import defaultdict
from math import floor, log, sqrt
import warnings


class LendingAMM:
    PREV_P_O_DELAY = 2 * 60  # seconds
    MAX_P_O_CHANGE = 1.25  # matches on-chain MAX_P_O_CHG / 1e18
    MIN_PRICE_RATIO = 1 / MAX_P_O_CHANGE

    def __init__(self, p_base: float, A: int, fee: float, dynamic_fee_multiplier: float | None = None):
        self.p_base = p_base
        self.p_oracle = p_base
        self.prev_p_oracle = p_base
        self.raw_p_oracle = p_base
        self.old_p_oracle = p_base
        self.old_dfee = 0.0
        self.prev_p_oracle_time: float | None = None
        self.current_timestamp: float | None = None
        self.A = A
        self.dynamic_fee_multiplier = dynamic_fee_multiplier if dynamic_fee_multiplier is not None else 0.25
        self.bands_x = defaultdict(float)
        self.bands_y = defaultdict(float)
        self.active_band = 0
        self.fee = fee

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
        price, _ = self._limit_price_oracle(p, timestamp, write=True)
        self.prev_p_oracle = self.p_oracle
        self.p_oracle = price

    def dynamic_fee(self, n_band, timestamp: float | None = None):
        """Replicates on-chain logic: max(base fee, oracle-memory fee, distance fee)."""
        p_oracle, oracle_memory_fee = self._price_oracle_view(timestamp)
        fee_with_memory = max(self.fee, oracle_memory_fee)
        distance_fee = self._distance_fee(p_oracle, n_band)
        return max(fee_with_memory, distance_fee)

    def _normalize_timestamp(self, timestamp: float | None) -> float | None:
        if timestamp is None:
            warnings.warn("Timestamp not provided. Oracle memory decay disabled; fee memory pinned at max.")
            return self.prev_p_oracle_time
        self.current_timestamp = timestamp
        return timestamp

    def _memory_dt(self, timestamp: float | None) -> float:
        if self.prev_p_oracle_time is None or timestamp is None:
            return self.PREV_P_O_DELAY
        elapsed = max(timestamp - self.prev_p_oracle_time, 0)
        return self.PREV_P_O_DELAY - min(self.PREV_P_O_DELAY, elapsed)

    def _limit_price_oracle(self, price: float, timestamp: float | None, write: bool) -> tuple[float, float]:
        timestamp = self._normalize_timestamp(timestamp)
        old_price = self.old_p_oracle
        old_dfee = self.old_dfee
        dt = self._memory_dt(timestamp)
        limited_price = price
        ratio = 0.0

        if dt > 0 and old_price > 0:
            price_ratio = min(old_price, price) / max(old_price, price)
            if price > old_price and price_ratio < self.MIN_PRICE_RATIO:
                price_ratio = self.MIN_PRICE_RATIO
                limited_price = old_price * self.MAX_P_O_CHANGE
            elif price < old_price and price_ratio < self.MIN_PRICE_RATIO:
                price_ratio = self.MIN_PRICE_RATIO
                limited_price = old_price / self.MAX_P_O_CHANGE

            ratio = ((1.0 + old_dfee) - price_ratio**3) * (dt / self.PREV_P_O_DELAY)
            ratio = min(max(ratio, 0.0), 1.0 - 1e-18)

        if write:
            self.raw_p_oracle = price
            self.old_p_oracle = limited_price
            self.old_dfee = ratio
            if timestamp is not None:
                self.prev_p_oracle_time = timestamp

        return limited_price, ratio

    def _price_oracle_view(self, timestamp: float | None) -> tuple[float, float]:
        price = self.raw_p_oracle if self.raw_p_oracle is not None else self.p_oracle
        return self._limit_price_oracle(price, timestamp, write=False)

    def _distance_fee(self, p_oracle: float, n_band: int) -> float:
        p_o_up = self.p_top(n_band)
        if p_o_up <= 0:
            return 0.0

        # Matches on-chain: p_c_d = p_o**3 / p_o_up**2, p_c_u = p_c_d * (A / (A-1))**2
        p_c_d = (p_oracle**3) / (p_o_up**2)
        band_ratio = self.A / (self.A - 1)
        p_c_u = p_c_d * band_ratio**2

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
            p_oracle = self.p_oracle
        k = (self.A - 1) / self.A  # equal to (p_down / p_up)
        p_base = self.p_base * k**n_band
        return p_oracle**3 / p_base**2

    def p_up(self, n_band, p_oracle: float | None = None):
        """
        Upper price for the band at the current p_oracle
        """
        if p_oracle is None:
            p_oracle = self.p_oracle
        k = (self.A - 1) / self.A  # equal to (p_down / p_up)
        p_base = self.p_base * k ** (n_band + 1)
        return p_oracle**3 / p_base**2

    def p_top(self, n):
        k = (self.A - 1) / self.A  # equal to (p_down / p_up)
        # Prices which show start and end of band when p_oracle = p
        return self.p_base * k**n

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
            assert self.bands_x[i] == 0
            self.bands_y[i] += y

    def deposit_nrange(self, amount, p, dn):
        n_top = self.get_band_n(self.p_oracle) + 1
        assert p <= self.p_oracle
        n1 = max(self.get_band_n(p), n_top)
        n2 = n1 + dn - 1
        y = amount / dn
        self.min_band = n1
        self.max_band = n2
        for i in range(n1, n2 + 1):
            assert self.bands_x[i] == 0
            self.bands_y[i] += y

    def get_y0(self, n=None):
        A = self.A
        if n is None:
            n = self.active_band
        x = self.bands_x[n]
        y = self.bands_y[n]
        p_o = self.p_oracle
        p_top = self.p_top(n)

        # solve:
        # p_o * A * y0**2 - y0 * (p_top/p_o * (A-1) * x + p_o**2/p_top * A * y) - xy = 0
        a = p_o * A
        b = p_top / p_o * (A - 1) * x + p_o**2 / p_top * A * y
        D = b**2 + 4 * a * x * y
        return (b + sqrt(D)) / (2 * a)

    def get_f(self, y0=None, n=None):
        if y0 is None:
            y0 = self.get_y0()
        if n is None:
            n = self.active_band
        p_top = self.p_top(n)
        p_oracle = self.p_oracle
        return y0 * p_oracle**2 / p_top * self.A

    def get_g(self, y0=None, n=None):
        if y0 is None:
            y0 = self.get_y0()
        if n is None:
            n = self.active_band
        p_top = self.p_top(n)
        p_oracle = self.p_oracle
        return y0 * p_top / p_oracle * (self.A - 1)

    def get_p(self, y0=None):
        x = self.bands_x[self.active_band]
        y = self.bands_y[self.active_band]
        if x == 0 and y == 0:
            return (self.p_up(self.active_band) * self.p_down(self.active_band)) ** 0.5
        if y0 is None:
            y0 = self.get_y0()
        return (self.get_f(y0) + x) / (self.get_g(y0) + y)

    def trade_to_price(self, price) -> tuple:
        """
        Not the method to be present in real smart contract, for simulations only
        Returns tuple of x and y changes in target band

        price is the external market price after external execution costs only.
        This method applies the AMM fee per band; callers must not pre-apply it.
        """

        if self.bands_x[self.active_band] == 0 and self.bands_y[self.active_band] == 0:
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

            x = self.bands_x[n]
            y = self.bands_y[n]

            if x == 0 and y == 0:
                if self.p_down(n) <= price <= self.p_up(n):
                    break
                self.active_band += bstep
                continue

            y0 = self.get_y0()
            g = self.get_g(y0)
            f = self.get_f(y0)
            # (f + x)(g + y) = const = p_oracle * A**2 * y0**2 = I
            Inv = (f + x) * (g + y)
            # p = (f + x) / (g + y) => p * (g + y)**2 = I or (f + x)**2 / p = I
            price = original_price

            fee = self.dynamic_fee(n, timestamp=self.current_timestamp)
            p_c_d = self.p_down(n)
            p_c_u = self.p_up(n)

            if bstep == 1:  # up
                price = price * (1 - fee)
                if price < p_c_d:
                    break

                # reduce y, increase x, go up
                y_dest = (Inv / price) ** 0.5 - g
                x_old = self.bands_x[n]
                if y_dest >= 0:
                    # End the cycle
                    self.bands_y[n] = y_dest
                    self.bands_x[n] = Inv / (g + y_dest) - f
                    delta_x = self.bands_x[n] - x_old
                    self.bands_x[n] += fee * delta_x
                    dx += self.bands_x[n] - x
                    dy += self.bands_y[n] - y
                    break

                else:
                    self.bands_y[n] = 0
                    self.bands_x[n] = Inv / g - f
                    delta_x = self.bands_x[n] - x_old
                    self.bands_x[n] += fee * delta_x
                    self.active_band += 1

            else:  # down
                price = price * (1 + fee)
                if price > p_c_u:
                    break

                # increase y, reduce x, go down
                x_dest = (Inv * price) ** 0.5 - f
                y_old = self.bands_y[n]
                if x_dest >= 0:
                    # End the cycle
                    self.bands_x[n] = x_dest
                    self.bands_y[n] = Inv / (f + x_dest) - g
                    delta_y = self.bands_y[n] - y_old
                    self.bands_y[n] += fee * delta_y
                    dx += self.bands_x[n] - x
                    dy += self.bands_y[n] - y
                    break

                else:
                    self.bands_x[n] = 0
                    self.bands_y[n] = Inv / f - g
                    delta_y = self.bands_y[n] - y_old
                    self.bands_y[n] += fee * delta_y
                    self.active_band -= 1

            dx += self.bands_x[n] - x
            dy += self.bands_y[n] - y

        return dx, dy

    def get_y_up(self, n):
        """
        Measure the amount of y in the band n if we adiabatically trade near p_oracle on the way up
        """
        x = self.bands_x[n]
        y = self.bands_y[n]
        p_o = self.p_oracle
        p_o_up = self.p_top(n)
        p_o_down = p_o_up * (self.A - 1) / self.A
        p_current_mid = p_o**3 / p_o_down**2 * (self.A - 1) / self.A
        sqrt_band_ratio = sqrt(self.A / (self.A - 1))

        if x == 0 or y == 0:
            if x == 0 and y == 0:
                return 0

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
        g = self.get_g(y0, n)
        f = self.get_f(y0, n)
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
        x = self.bands_x[n]
        y = self.bands_y[n]
        p_o = self.p_oracle
        p_o_up = self.p_top(n)
        p_o_down = p_o_up * (self.A - 1) / self.A
        p_current_mid = p_o**3 / p_o_down**2 * (self.A - 1) / self.A
        sqrt_band_ratio = sqrt(self.A / (self.A - 1))

        if x == 0 or y == 0:
            if x == 0 and y == 0:
                return 0

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
        g = self.get_g(y0, n)
        f = self.get_f(y0, n)
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

    def get_all_y(self):
        return sum(self.get_y_up(i) for i in range(-500, 500))

    def get_all_x(self):
        return sum(self.get_x_down(i) for i in range(-500, 500))
