# Native replay: readability review

This branch organizes the performance experiment into focused commits for review. It is based on Curve master `0bb370f02970c2c1056da8a4faffd0fa9be35575`. It is not a proposal to merge every change as one upstream PR.

## Assessment

The performance gain is substantial, but the combined source is more intrusive than a lightweight compilation layer. Helper extraction and explicit quote snapshots are the strongest candidates for an upstream discussion. Custom storage, global caches, optional-value adapters and changed configuration behavior need a higher acceptance bar. Smaller, separately justified patches would be easier to maintain and review.

| Change | Readability and compatibility cost | Assessment |
|---|---|---|
| Extract numeric replay and oracle helpers | Moves existing calculations and adds a public-wrapper/private-helper boundary; indexed float64 inputs are less general than arbitrary Python sequences | Reasonable boundary for native compilation; keep the main replay easy to follow |
| Native math | Ordinary `**` expressions, a checked square-root wrapper and a Cython dependency | Power wrappers and source decorators have been removed; the external native profile uses `cpow: true` |
| Cache dimensionless band factors and last-value powers | Adds bounded module-level state keyed by its actual inputs; the global spot-price cache has been removed | Reduces repeated work across positions while simplifying the earlier cache design |
| Skip searches blocked by fees or empty-band bounds | Adds two explicit preconditions and a shared price/bounds helper | Improves both Python and native replay without another cache; retain tests for tiny exchanges and oracle writes |
| Reuse balances and band top in numeric helpers | Passes already-loaded inputs to the unchanged invariant and virtual-balance equations; internal calls bypass public `get_y0()` overrides | Small local refactor with no new cache, about 6% less replay time in paired comparisons |
| Numeric band storage | Dense presence flags and bounds replace the Python key set; C-array layout stays in declarations | Preserves zero keys, overflow and ascending valuation; still a custom container with `.read()` / `.write()` calls |
| Numeric oracle state | Adds optional-value properties and a small public-to-numeric boundary | Preserves `None` clocks and raw-price fallback; explicitly invalid timestamps now fail before observation writes |
| Per-instance oracle settings | Moves configuration defaults from class attributes to instance fields | Changes class-wide override behavior; should not be presented as a neutral refactor |
| Reset and numeric batch replay | Adds state-reset obligations and an eight-column resolved-task interface | Useful for throughput; the batch contract is currently specific to the benchmark |

The underlying accounting remains in Python. The branch adds no handwritten C++ implementation. That does not make every change behaviorally equivalent for every Python caller: native integer limits, custom storage, float64 input conversion, final compiled classes and configuration overrides narrow the supported interface. Replay and exchanges share `_trade_prices()` internally; overriding only `get_p()` no longer changes those internal quotes. Internal trading and valuation also call `_get_y0()` directly, so overriding only public `get_y0()` no longer intercepts those calculations.

The batch entry uses unshifted positions and default opening oracle state. Its docstring defines every input column, including the unused eighth slot retained for benchmark compatibility. Each task supplies its external fee explicitly; neither a successful nor a failed batch changes the simulator's configured fee. Reset clears the previous position bounds as well as balances and oracle memory. This remains a resolved-task interface, not a general replacement for the simulator API. The short declarations under `declarations/` are included so reviewers can inspect the complete native typing assumptions, including final classes and scalar field types.

## Measured result

The integrated source was qualified at benchmark commit `37b552e400434cec2a6c10795f6fbb30c2b11e2f`. Python remains the only implementation of the financial calculations. The source-only branch carries matching simulator, test and declaration files; the runnable builder and evidence remain in the separate benchmark kit.

| Implementation | One worker | Four workers |
|---|---:|---:|
| Curve Python | 6.494461 s | 1.764144 s |
| Unchanged-source Cython | 2.404675 s | 0.676948 s |
| Fork Python | 2.932965 s | 0.812735 s |
| Fork Cython with PGO | **0.060101 s** | **0.019974 s** |
| Phil C++ with older accounting | 0.062182 s | 0.018946 s |

Five interleaved trials on one host; totals sum ETH/BTC/LP medians over 11,520 fixed replays and 518,400 candles. Inputs are resident; dispatch and return are timed, startup/preparation excluded. This is approximately Phil's speed: about 3% less time with one worker and 5% more with four. Small timing margins are not a claim of a decisive lead. Phil's accounting differs and remains a speed reference only. The historical LP denomination mismatch remains in the frozen benchmark; this is not a calibration or end-to-end application measurement.

A separate paired comparison with our immediately preceding candidate measured **44.8% less native time** and **6.0% less interpreted Python time**. The integration also takes 7.5% less native time than the coworker's proposal in that paired session, retaining our newer shared inputs and raw-target guards. Do not compare absolute timings across separate sessions.

All 30 ordered output arrays match the previous qualification byte for byte. Fork Python matches Curve Python exactly; compiled maximum loss error is unchanged at `4.007905118896815e-14`, below `5e-14`. Repeat runs and one/four-worker outputs agree bitwise. All 57 source tests, 14 focused compiled tests and 48 short pipeline fixtures passed. The 156 state cases and 2,436 compiled replay prefixes match the previous candidate exactly.

Two clean builds produced identical generated C++, merged PGO profiles and extension binaries on the pinned environment. PGO uses a fixed synthetic training workload, never the benchmark inputs. The full campaign now has eight fewer production Python lines than the original review candidate; line count does not remove the custom-storage and native-interface tradeoffs.

The timestamp fix distinguishes an omitted `None` from an explicitly supplied nonfinite value. Both direct observations and batch replay reject invalid times before writing that observation. Public timestamp properties and timestamped quotes also reject nonfinite values instead of letting the internal missing-clock marker escape into the public interface. `raw_p_oracle=None` retains its existing fallback. Private numeric methods are an internal compilation boundary, not a drop-in optional-value API.

## Build declarations

`declarations/lending_amm.pxd` and `declarations/simulator.pxd` record the current native type declarations, including the explicit per-task fee argument. An external build package stages them alongside the matching `.py` files, then generates and compiles C++. They are review inputs here, not an automatically enabled build or a second accounting implementation.

The measured environment was CPython 3.11.15, Cython 3.2.4 and Apple Clang 21 with SDK 26.5. The native profile requires `cpow: true`, uses `-std=c++17 -O3 -g0 -ffp-contract=off -fno-fast-math`, and adds profile-guided optimization during compilation. The builder records a pinned matching `llvm-profdata`, training source and generated profile hashes. Declarations alone are not a complete build recipe; use the matching benchmark kit revision. Contributors can establish baselines on their own hardware with an explicitly recorded target.

The build layer stages committed Python without rewriting it. Compatible source commits reuse the driver and profile. Changed typed signatures or layouts may require declaration review; ordinary source edits do not require bespoke conversion code. Routine checks are short; qualify release/toolchain reproducibility with two clean builds, without fresh calibration sweeps.

Before an upstream submission, separate broadly useful Python changes from native-specific restrictions and simplify the visible accounting operations where possible. The measured gains justify continued work; custom storage and API changes still deserve explicit maintainer review.
