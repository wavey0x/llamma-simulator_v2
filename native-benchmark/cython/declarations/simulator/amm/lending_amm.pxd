# Types and native dispatch only. Financial formulas live in the pinned .py file.
import cython

cdef class LendingAMM:
    cdef dict __dict__
    cdef public object A
    cdef public object bands_x, bands_y
    cdef public object active_band
    cdef public object current_timestamp, prev_p_oracle_time, raw_p_oracle
    cdef public double p_base, p_oracle, prev_p_oracle, old_p_oracle, old_dfee
    cdef public double fee, dynamic_fee_multiplier

    cpdef set_p_oracle(self, p, timestamp=*)
    @cython.locals(p_oracle=cython.double, oracle_memory_fee=cython.double,
                   fee_with_memory=cython.double, distance_fee=cython.double)
    cpdef double dynamic_fee(self, n_band, timestamp=*) except *
    cpdef _normalize_timestamp(self, timestamp)
    @cython.locals(elapsed=cython.double)
    cpdef double _memory_dt(self, timestamp) except *
    @cython.locals(old_price=cython.double, old_dfee=cython.double, dt=cython.double,
                   limited_price=cython.double, ratio=cython.double, price_ratio=cython.double)
    cpdef (double, double) _limit_price_oracle(self, double price, timestamp)
    @cython.locals(price=cython.double)
    cpdef (double, double) _price_oracle_view(self, timestamp)
    @cython.locals(p_o_up=cython.double, p_c_d=cython.double, band_ratio=cython.double, p_c_u=cython.double)
    cpdef double _distance_fee(self, double p_oracle, n_band) except *
    cpdef double p_down(self, n_band, p_oracle=*) except *
    cpdef double p_up(self, n_band, p_oracle=*) except *
    cpdef double p_top(self, n) except *
    cpdef double p_bottom(self, n) except *
    cpdef get_band_n(self, p)
    cpdef deposit_range(self, amount, p1, p2)
    cpdef deposit_nrange(self, amount, p, dn)
    @cython.locals(A=cython.double, x=cython.double, y=cython.double, p_o=cython.double,
                   p_top=cython.double, a=cython.double, b=cython.double, D=cython.double)
    cpdef double get_y0(self, n=*) except *
    cpdef double get_f(self, y0=*, n=*) except *
    cpdef double get_g(self, y0=*, n=*) except *
    @cython.locals(x=cython.double, y=cython.double)
    cpdef double get_p(self, y0=*) except *
    @cython.locals(x=cython.double, y=cython.double, y0=cython.double, f=cython.double, g=cython.double,
                   p_o=cython.double, current_price=cython.double,
                   dx=cython.double, dy=cython.double, dfee=cython.double)
    cpdef tuple trade_to_price(self, price)
    @cython.locals(x=cython.double, y=cython.double, p_o=cython.double, p_o_up=cython.double,
                   p_o_down=cython.double, p_current_mid=cython.double, sqrt_band_ratio=cython.double,
                   y_equiv=cython.double, x_equiv=cython.double, y0=cython.double, g=cython.double,
                   f=cython.double, Inv=cython.double, y_o=cython.double, x_o=cython.double)
    cpdef double get_y_up(self, n) except *
    @cython.locals(x=cython.double, y=cython.double, p_o=cython.double, p_o_up=cython.double,
                   p_o_down=cython.double, p_current_mid=cython.double, sqrt_band_ratio=cython.double,
                   y_equiv=cython.double, x_equiv=cython.double, y0=cython.double, g=cython.double,
                   f=cython.double, Inv=cython.double, y_o=cython.double, x_o=cython.double)
    cpdef double get_x_down(self, n) except *
