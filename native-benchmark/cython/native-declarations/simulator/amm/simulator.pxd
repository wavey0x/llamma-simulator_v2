import cython
from simulator.amm.lending_amm cimport LendingAMM, find_target_price

@cython.locals(amm=LendingAMM, i=cython.Py_ssize_t, timestamp=cython.double,
               oracle_price=cython.double, p0=cython.double, initial_y0=cython.double,
               initial_x_value=cython.double, initial_all_x=cython.double, t=cython.double,
               high=cython.double, low=cython.double, high_external=cython.double,
               low_external=cython.double, loss=cython.double, external_fee=cython.double,
               log_enabled=cython.bint, verbose=cython.bint)
cpdef double _calculate_loss(
    simulator, double A, double fee,
    const double[:, :] prices_for_simulation, const double[:] oracle_prices_for_simulation,
    long initial_liquidity_range, dynamic_fee_multiplier=*, double position_shift=*,
    initial_state=*, workspace=*, external_fee_override=*
) except? -1

@cython.locals(i=cython.Py_ssize_t, lo=cython.Py_ssize_t, hi=cython.Py_ssize_t,
               output=cython.double[:], workspace=LendingAMM)
cpdef replay_batch(simulator, const double[:, :] points, const double[:, :] records)
