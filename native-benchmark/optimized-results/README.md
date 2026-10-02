# Optimized replay results — 2026-10-01

**52.6x Curve Python throughput, with Phil still 1.99x faster**, on the same one-worker replay workload. The interpreted fork takes 44.7% less time than Curve Python. These results qualify the current source; they do not establish arbitrary Python API equivalence or end-to-end application performance.

## Measurement

Five interleaved trials on Apple M1 Max; 11,520 frozen ETH/BTC/LP windows and 518,400 candles. Times sum the three per-market medians. Inputs are resident; dispatch and ordered result return are included, preparation/startup/warmup excluded. The [protocol](../README.md#what-this-measures) describes the historical LP denomination mismatch and fixed replay assumptions.

| Backend | 1 worker | 4 workers |
|---|---:|---:|
| Curve Python | 6.470050 s | 1.708603 s |
| Unchanged-source Cython | 2.447006 s | 0.645616 s |
| Fork Python | 3.577362 s | 0.958143 s |
| Fork native Cython | **0.122925 s** | **0.034846 s** |
| Phil C++ with older accounting | 0.061741 s | 0.018584 s |

Use [summary.json](summary.json) for per-market medians/ranges, [timings.json](timings.json) for all 150 trial records, [comparison.json](comparison.json) for errors and [receipt.json](receipt.json) for identities and output hashes. The `.npy` files retain all ordered loss arrays. Establish fresh local baselines when assessing another optimization; absolute times from different sessions or hosts are not directly comparable.

## Correctness and build identity

- Fork Python matches Curve Python bit for bit in all 11,520 windows. Native maximum absolute loss error is `4.007905118896815e-14`, within the unchanged `5e-14` limit. Repeated and one/four-worker outputs match bitwise within each backend.
- Phil's older fee/oracle/recovery accounting differs beyond tolerance in every window. Its timing is a speed reference, never the correctness target.
- The timed source is `7017a4cda62184ea6d0283445067323aa119bfba`, present in this kit's history. Its [release receipt](qualification/release.json) records identical generated C++ and extension binaries from two clean builds. [Build identity](qualification/candidate/build.json) and [48 short replay/state/error checks](qualification/validation/verification.json) are included.
- The source-only review branch is pinned at `1505d10de6d74c13b58b16181bf8806f8c1d5ccd`. Its simulator, tests and review declarations match the timed source byte for byte. Rebuilding that exact commit produced identical generated C++ and extension hashes and passed the short gate; see its [release receipt](published-source/release.json). Its 52 Python tests and nine focused compiled tests passed.

The build ID changes with source provenance; the executable artifact hash remains `a0ee5c7cd186c5a777f3ad64305db5d8ce3b3df5afc28bcb6a7f5d876d139602`. The compiler recipe is unchanged: CPython 3.11.15, Cython 3.2.4, Apple Clang 21, SDK 26.5, `-std=c++17 -O3 -g0 -ffp-contract=off -fno-fast-math`.

## Retained changes

Native oracle snapshots avoid Python tuple/float allocation. Geometry caches only dimensionless band factors, uses fixed native arrays and no longer resets for every base price. The global spot-price cache is removed. Minimum-fee checks and shared raw-price bounds avoid unnecessary band searches without new persistent cache state. Together these changes remove 41 production Python lines relative to the earlier review candidate; tests and declarations add separate coverage and typing.

A faster fee-adjusted empty-band shortcut was rejected: it passed market outputs but skipped tiny exchanges that write oracle memory. The retained raw-price check preserves those writes, covered by `tests/test_empty_band_quotes.py`. Additional probes matched the preceding candidate across 156 state cases and 2,436 compiled replay prefixes. This is why targeted checks follow each changed behavior; no new calibration sweep was needed.
