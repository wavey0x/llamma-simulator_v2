# Types and native dispatch only. Financial formulas live in the pinned .py file.
import cython

ctypedef (double, double) OracleSnapshot

cpdef double sqrt(double value) except? -1

cpdef double fee_multiplier(double fee) except? -1

cdef double _factor_As[1002], _factors[1002]
@cython.locals(i=cython.long)
cpdef double _band_factor(double A, long n) except? -1
cdef double _cube_price, _cube_value, _ratio_A, _ratio_value
cpdef double _cube(double price) except? -1
cpdef double _ratio_square(double A) except? -1

@cython.final
cdef class BandBalances:
    cdef double _values[1001]
    cdef signed char _present[1001]
    cdef readonly long lowest, highest
    cdef dict _overflow
    cpdef double read(self, long n) except? -1
    cpdef void write(self, long n, double value) except *
    cpdef bint has(self, long n) except -1
    cdef void _represent(self, long n) noexcept
    @cython.locals(n=cython.long)
    cpdef void clear(self) except *

@cython.final
cdef class LendingAMM:
    cdef dict __dict__
    cdef public double PREV_P_O_DELAY, MAX_P_O_CHANGE, MIN_PRICE_RATIO
    cdef public double A
    cdef public BandBalances bands_x, bands_y
    cdef public long min_band, max_band
    cdef public long active_band
    cdef public object current_timestamp, prev_p_oracle_time, raw_p_oracle
    cdef public double p_base, p_oracle, prev_p_oracle, old_p_oracle, old_dfee
    cdef public double fee, dynamic_fee_multiplier

    cpdef void reset(self, double p_base, double A, double fee, dynamic_fee_multiplier=*, oracle_state=*) except *
    @cython.locals(price=cython.double, fee=cython.double, timestamp=cython.double)
    cpdef void restore_oracle_state(self, state) except *
    cpdef void set_p_oracle(self, double p, timestamp=*)
    @cython.locals(snapshot=OracleSnapshot)
    cpdef (double, double) _observe(self, double p, timestamp=*)
    @cython.locals(p_oracle=cython.double, oracle_memory_fee=cython.double,
                   fee_with_memory=cython.double, distance_fee=cython.double)
    cpdef double dynamic_fee(self, long n_band, timestamp=*) except? -1
    @cython.locals(fee_with_memory=cython.double, distance_fee=cython.double)
    cpdef double _dynamic_fee(self, long n_band, double p_oracle, double oracle_memory_fee) except? -1
    cpdef _normalize_timestamp(self, timestamp)
    @cython.locals(elapsed=cython.double, current=cython.double, previous=cython.double, delay=cython.double)
    cpdef double _memory_dt(self, timestamp) except? -1
    @cython.locals(old_price=cython.double, old_dfee=cython.double, dt=cython.double,
                   limited_price=cython.double, ratio=cython.double, price_ratio=cython.double)
    cpdef (double, double) _limit_price_oracle(self, double price, timestamp)
    @cython.locals(price=cython.double)
    cpdef (double, double) _price_oracle_view(self, timestamp)
    @cython.locals(p_o_up=cython.double, p_c_d=cython.double, band_ratio=cython.double, p_c_u=cython.double)
    cpdef double _distance_fee(self, double p_oracle, long n_band) except? -1
    @cython.locals(k=cython.double, p_base=cython.double, price=cython.double)
    cpdef double p_down(self, long n_band, p_oracle=*) except? -1
    @cython.locals(k=cython.double, p_base=cython.double, price=cython.double)
    cpdef double p_up(self, long n_band, p_oracle=*) except? -1
    @cython.locals(k=cython.double)
    cpdef double p_top(self, long n) except? -1
    @cython.locals(k=cython.double)
    cpdef double p_bottom(self, long n) except? -1
    @cython.locals(k=cython.double)
    cpdef long get_band_n(self, double p) except? -1
    @cython.locals(n1=cython.long, n2=cython.long, i=cython.long, y=cython.double)
    cpdef deposit_range(self, double amount, double p1, double p2)
    @cython.locals(n_top=cython.long, n1=cython.long, n2=cython.long, i=cython.long, y=cython.double)
    cpdef deposit_nrange(self, double amount, double p, long dn)
    @cython.locals(band=cython.long, x=cython.double, y=cython.double)
    cpdef double get_y0(self, n=*) except? -1
    @cython.locals(A=cython.double, p_o=cython.double,
                   a=cython.double, b=cython.double, D=cython.double)
    cpdef double _get_y0(self, double x, double y, double p_top) except? -1
    @cython.locals(band=cython.long, value=cython.double, p_top=cython.double, p_oracle=cython.double)
    cpdef double get_f(self, y0=*, n=*) except? -1
    @cython.locals(band=cython.long, value=cython.double, p_top=cython.double, p_oracle=cython.double)
    cpdef double get_g(self, y0=*, n=*) except? -1
    cpdef double _get_f(self, double value, double p_top) except? -1
    cpdef double _get_g(self, double value, double p_top) except? -1
    cpdef double get_p(self, y0=*) except? -1
    @cython.locals(price=cython.double, value=cython.double, x=cython.double, y=cython.double, p_top=cython.double,
                   lower_price=cython.double, upper_price=cython.double)
    cpdef (double, double, double) _trade_prices(self, y0=*)
    cpdef void _commit_oracle(self, (double, double) snapshot)
    @cython.locals(x=cython.double, y=cython.double, y0=cython.double, f=cython.double, g=cython.double,
                   p_o=cython.double, p_top=cython.double, lower_price=cython.double, upper_price=cython.double,
                   current_price=cython.double,
                   dx=cython.double, dy=cython.double, oracle_memory_fee=cython.double, fee=cython.double,
                   snapshot=OracleSnapshot,
                   antifee=cython.double,
                   n=cython.long, original_band=cython.long, bstep=cython.long, Inv=cython.double,
                   original_price=cython.double, x_dest=cython.double, y_dest=cython.double,
                   x_old=cython.double, y_old=cython.double, delta_x=cython.double, delta_y=cython.double)
    cpdef (double, double) trade_to_price(self, double price)
    @cython.locals(x=cython.double, y=cython.double, p_o=cython.double, p_o_up=cython.double,
                   p_o_down=cython.double, p_current_mid=cython.double, sqrt_band_ratio=cython.double,
                   y_equiv=cython.double, x_equiv=cython.double, y0=cython.double, g=cython.double,
                   f=cython.double, Inv=cython.double, y_o=cython.double, x_o=cython.double)
    cpdef double get_y_up(self, n) except? -1
    @cython.locals(x=cython.double, y=cython.double, p_o=cython.double, p_o_up=cython.double,
                   p_o_down=cython.double, p_current_mid=cython.double, sqrt_band_ratio=cython.double,
                   y_equiv=cython.double, x_equiv=cython.double, y0=cython.double, g=cython.double,
                   f=cython.double, Inv=cython.double, y_o=cython.double, x_o=cython.double)
    cpdef double get_x_down(self, n) except? -1

    @cython.locals(i=cython.long, total=cython.double, x=BandBalances, y=BandBalances)
    cpdef double get_all_x(self) except? -1
    @cython.locals(i=cython.long, total=cython.double, x=BandBalances, y=BandBalances)
    cpdef double get_all_y(self) except? -1

@cython.locals(n=cython.long, p_down=cython.double, p_up=cython.double, target=cython.double,
    p_oracle=cython.double, memory_fee=cython.double)
cpdef double find_target_price(LendingAMM amm, double p, double p_oracle, double memory_fee, bint is_up=*) except? -1
