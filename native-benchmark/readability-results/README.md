# Readability cleanup validation

Source: `54544eeb08393b0f91e6ced10c13d9405a9b9ebb`, compared with qualified candidate `37b552e400434cec2a6c10795f6fbb30c2b11e2f`. Build kit: `6dbca50`.

The cleanup uses Curve's formatting and mathematical names, names the batch arguments and valuation bounds, allocates band buffers once in the constructor, and removes 13 stale local declarations. Financial expressions and their evaluation order are retained. Batch calls specify every optional argument so Cython keeps direct native dispatch. The builder, profile, synthetic PGO training, compiler flags and toolchain are unchanged.

| Backend | Workers | Previous | Cleanup |
|---|---:|---:|---:|
| Python | 1 | 5.426122 s | 5.045722 s |
| Python | 4 | 2.724023 s | 2.680139 s |
| Cython + PGO | 1 | 0.125936 s | 0.119345 s |
| Cython + PGO | 4 | 0.063239 s | 0.062006 s |

Five interleaved Python trials and seven native trials, using the existing benchmark worker and all 11,520 frozen ETH/BTC/LP replays. Each configuration receives two warmups per market. Totals sum market medians; resident-input dispatch and return are timed, preparation/startup excluded. These paired timings show no overall regression. Absolute timings vary with host conditions; compare within this session, not against older sessions. Per-market ranges and all trials are in [summary.json](summary.json) and [timings.json](timings.json).

All 24 ordered output arrays match the previous candidate byte for byte. One/four-worker outputs agree, and the existing worker checks repeatability on every trial. Python matches Curve Python exactly; native maximum absolute loss error is unchanged at `4.007905118896815e-14`, below `5e-14`. [checks.json](checks.json) records identities and hashes of the raw float64 output payloads; the identical arrays are already available in the [previous qualification](../integrated-results/).

All 57 Python tests, 14 focused compiled tests and 48 short pipeline fixtures passed. The compiled tests cover storage, reset, batch fee isolation, geometry, empty-band boundaries and oracle behavior. [release.json](release.json), [build.json](build.json) and [verification.json](verification.json) identify the final artifact and numerical checks. This routine check did not repeat the earlier two-clean-build qualification or run a new calibration sweep.

Use the [benchmark instructions](../README.md#reproduce) with the source commit above and this revision's declarations. Native declarations remain in the external build kit, outside the upstream PR.
