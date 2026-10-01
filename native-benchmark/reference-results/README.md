# Handoff verification — 2026-10-01

The standalone kit was executed with a fresh locked environment and fresh baseline/candidate builds. The source under test is `3f2f95d1e1528cc17bcd7b2454c7c76b28c0fd18`; this packaging change adds no financial logic. All 48 existing Python tests passed. Both builds passed the 48-window compiled/Python fixtures.

Five interleaved trials on Apple M1 Max, one/four process workers, 11,520 frozen windows and 518,400 candles. Totals below sum three per-market medians. Preparation, startup and warmup are excluded; dispatch and ordered output return are included.

| Backend | 1 worker | 4 workers |
|---|---:|---:|
| Curve Python | 6.460046 s | 1.736274 s |
| Unchanged-source Cython | 2.416468 s | 0.647700 s |
| Fork Python | 7.050255 s | 1.882544 s |
| Fork native Cython | 0.238051 s | 0.065152 s |
| Phil C++ with older accounting | 0.062052 s | 0.018364 s |

All 30 ordered loss arrays are bitwise identical to the previous five-backend harness's outputs. Fork Python matches untouched Curve Python exactly. Maximum Cython loss error is `4.007905118896815e-14`, below the unchanged `5e-14` tolerance. Repeats and worker counts match bitwise within each backend. Phil differs beyond tolerance in every window and remains only a speed reference.

The native candidate's build ID, generated C++ hashes and extension hashes exactly match the previously qualified cleanup build (`a99946a408ee4209df55a6f9ab988f90f4d334a956c41c44f1cbc61935953c9c`). This check verifies relocation of the existing builder; it is not a new two-clean-build qualification request.

Use [summary.json](summary.json) for per-market medians/ranges, [timings.json](timings.json) for all 150 trial records, [comparison.json](comparison.json) for numerical errors and [receipt.json](receipt.json) for the benchmark identity. The `baseline-*` and `candidate-*` files record source/build identities and short-fixture results. The `.npy` files preserve ordered outputs. Re-run all backends locally before assessing a new optimization; do not compare absolute times from different sessions or hosts.
