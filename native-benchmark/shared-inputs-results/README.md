# Shared-input replay results — 2026-10-01

The latest refactor reuses loaded balances and band top in the unchanged invariant and virtual-balance equations. It adds no cache state and reduces replay time by about **6%** in paired comparisons. The full native candidate reaches **55.7x Curve Python throughput**, with Phil still **1.87x faster**, on the same one-worker workload.

## Incremental improvement

Each row compares before/after in one session on all 11,520 frozen windows with one worker. `standard` is the preceding published implementation; `geometry` shares balances and band top. Native measurements use five interleaved trials of three batches; Python uses three trials of one batch. Compare within rows, not across sessions.

| Backend | Before | After | Less time |
|---|---:|---:|---:|
| Native Cython | 0.123342 s | 0.115750 s | 6.2% |
| Python | 2.794345 s | 2.637224 s | 5.6% |

Raw [native trials](paired/native/trials.json) and [Python trials](paired/python/trials.json) accompany their summaries. The Python trial file also retains the intermediate `balances` variant, which reused balances without sharing band top; it is not the final candidate.

## Common benchmark

Five interleaved trials on Apple M1 Max; 11,520 frozen ETH/BTC/LP windows and 518,400 candles. Times sum the three per-market medians. Inputs are resident; dispatch and ordered result return are included, preparation/startup/warmup excluded. The [protocol](../README.md#what-this-measures) describes the historical LP denomination mismatch and fixed replay assumptions.

| Backend | 1 worker | 4 workers |
|---|---:|---:|
| Curve Python | 5.074351 s | 1.371780 s |
| Unchanged-source Cython | 1.895407 s | 0.516307 s |
| Fork Python | 2.637599 s | 0.720178 s |
| Fork native Cython | **0.091151 s** | **0.026461 s** |
| Phil C++ with older accounting | 0.048699 s | 0.014859 s |

The interpreted fork takes **48.0% less time than Curve Python**. Phil remains 1.87x faster with one worker and 1.78x with four, but its older accounting differs beyond tolerance in every window. It is a speed reference, not a correctness target. These measurements do not establish end-to-end application performance.

Use [summary.json](summary.json) for per-market medians/ranges, [timings.json](timings.json) for all 150 trial records, [comparison.json](comparison.json) for errors and [receipt.json](receipt.json) for identities and output hashes. The `.npy` files retain all ordered loss arrays. The [preceding qualification](../optimized-results/README.md) is preserved; host timing shifted between sessions, so use the paired table above to assess this change's gain.

## Correctness and build identity

- All 30 ordered output arrays are byte-identical to the preceding qualification, with unchanged driver and workload hashes. Fork Python matches Curve Python bit for bit in all 11,520 windows. Native maximum absolute loss error remains `4.007905118896815e-14`, below the unchanged `5e-14` limit. Repeated and one/four-worker outputs match bitwise within each backend.
- The timed source is `098ce5fa311dd0dbe84eca0ac6f1619cab390a74`, present in this kit's history. Its [release receipt](qualification/release.json) records identical generated C++ and extension binaries from two clean builds. [Build identity](qualification/candidate/build.json) and the [48 short replay/state/error checks](qualification/validation/verification.json) are included.
- The source-only review branch is pinned at `f60abdfec7cb02c93f5b1c36476fed2137bb0de7`. Its simulator, tests and review declarations match the timed source byte for byte. Rebuilding that exact commit passed the short gate and produced identical generated C++ and extension hashes; see its [release receipt](published-source/release.json).
- The qualified source passed 52 Python tests and nine focused compiled tests. Additional checks matched the preceding implementation across 156 state cases and 2,436 compiled replay prefixes. These are bounded checks, not a proof of every possible Python input; no new calibration sweep was needed.

The executable artifact hash is `0924a15846af5155fb48cb6d86772dad8a0d07c2538e857e985d459e35d52ef8`. Build IDs differ with source provenance. The recipe is unchanged: CPython 3.11.15, Cython 3.2.4, Apple Clang 21, SDK 26.5, `-std=c++17 -O3 -g0 -ffp-contract=off -fno-fast-math`.

The latest source change adds two net Python lines; the campaign still removes 39 production Python lines overall relative to the earlier review candidate. Public signatures remain unchanged, but internal trading and valuation now call the private invariant solver directly: overriding only public `get_y0()` no longer intercepts those calculations. The [source review](../../benchmarks/native/README.md) records this and the existing storage, typing and configuration tradeoffs.
