# Native replay: readability review

This branch organizes the performance experiment into focused commits for review. It is based on Curve master `0bb370f02970c2c1056da8a4faffd0fa9be35575`. It is not a proposal to merge every change as one upstream PR.

## Assessment

The performance gain is substantial, but the combined source is more intrusive than a lightweight compilation layer. Helper extraction and explicit quote snapshots are the strongest candidates for an upstream discussion. Custom storage, global caches, native math wrappers and changed configuration behavior need a higher acceptance bar. Smaller, separately justified patches would be easier to maintain and review.

| Change | Readability and compatibility cost | Assessment |
|---|---|---|
| Extract numeric replay and oracle helpers | Moves existing calculations and adds a public-wrapper/private-helper boundary; indexed float64 inputs are less general than arbitrary Python sequences | Reasonable boundary for native compilation; keep the main replay easy to follow |
| Preserve native math behavior | Adds `_power(...)` calls, a square-root wrapper and a Cython runtime dependency to ordinary Python | Necessary for this measured build's rounding behavior; visibly obscures some equations |
| Cache dimensionless band factors and last-value powers | Adds bounded module-level state keyed by its actual inputs; the global spot-price cache has been removed | Reduces repeated work across positions while simplifying the earlier cache design |
| Skip searches blocked by fees or empty-band bounds | Adds two explicit preconditions and a shared price/bounds helper | Improves both Python and native replay without another cache; retain tests for tiny exchanges and oracle writes |
| Reuse balances and band top in numeric helpers | Passes already-loaded inputs to the unchanged invariant and virtual-balance equations; internal calls bypass public `get_y0()` overrides | Small local refactor with no new cache, about 6% less replay time in paired comparisons |
| Numeric band storage | Replaces `defaultdict` with a custom container and changes indexing to `.read()` / `.write()` throughout accounting | Largest maintenance and aesthetic cost; not a complete dictionary substitute |
| Per-instance oracle settings | Moves configuration defaults from class attributes to instance fields | Changes class-wide override behavior; should not be presented as a neutral refactor |
| Reset and numeric batch replay | Adds state-reset obligations and an eight-column resolved-task interface | Useful for throughput; the batch contract is currently specific to the benchmark |

The underlying accounting remains in Python. The branch adds no handwritten C++ implementation. That does not make every change behaviorally equivalent for every Python caller: native integer limits, custom storage, float64 input conversion, final compiled classes and configuration overrides narrow the supported interface. Replay and exchanges share `_trade_prices()` internally; overriding only `get_p()` no longer changes those internal quotes. Internal trading and valuation also call `_get_y0()` directly, so overriding only public `get_y0()` no longer intercepts those calculations.

The batch entry uses unshifted positions and default opening oracle state. Its docstring defines every input column, including the unused eighth slot retained for benchmark compatibility. Each task supplies its external fee explicitly; neither a successful nor a failed batch changes the simulator's configured fee. Reset clears the previous position bounds as well as balances and oracle memory. This remains a resolved-task interface, not a general replacement for the simulator API. The short declarations under `declarations/` are included so reviewers can inspect the complete native typing assumptions, including final classes and scalar field types.

## Measured result

These measurements cover the current simulator source and declarations, qualified at benchmark commit `098ce5fa311dd0dbe84eca0ac6f1619cab390a74`. The source-review history carries identical simulator, test and declaration files without the benchmark kit. Five interleaved trials used 11,520 frozen ETH/BTC/LP windows and 518,400 candles on Apple M1 Max. Times sum the three per-market medians; dispatch and result return are included, preparation and startup excluded.

| Implementation | One worker | Four workers |
|---|---:|---:|
| Upstream Curve Python | 5.074351 s | 1.371780 s |
| Unchanged-source Cython | 1.895407 s | 0.516307 s |
| Fork Python | 2.637599 s | 0.720178 s |
| Fork native Cython | 0.091151 s | 0.026461 s |
| Phil C++ with older accounting | 0.048699 s | 0.014859 s |

With one worker, the native candidate is **55.7x faster than Curve Python**, **20.8x faster than unchanged-source Cython**, and takes **1.87x Phil's time**. The interpreted fork takes **48.0% less time than Curve Python**. Phil's older accounting differs in every benchmark window, so its timing is a speed reference rather than an equivalent substitute. These historical fixtures retain the LP spot/oracle denomination mismatch; they measure replay performance, not a new calibration or complete application run.

Fork Python matches all 11,520 upstream losses bit for bit. The compiled candidate stays within the existing `5e-14` absolute loss tolerance (maximum `4.01e-14`). Repeated and one/four-worker outputs are bitwise equal within each backend. Two clean builds produced identical generated C++ and extension binaries on the pinned target. All 52 source tests, nine focused compiled tests and the pipeline's 48 short replay/state/error fixtures passed.

The latest campaign removes 39 production Python lines overall relative to the previous review candidate, while adding regression coverage. It keeps oracle snapshots native, caches only dimensionless geometry, removes the global spot-price cache and skips demonstrably unnecessary searches. The latest refactor reuses loaded balances and band top in the unchanged invariant and virtual-balance equations. Paired comparisons measured about 6% less replay time in both Python and Cython; absolute times from separate sessions should not be used to estimate that improvement. All 30 ordered output arrays remain byte-identical to the preceding qualification. The builder and strict compiler flags are unchanged.

The empty-band check deliberately compares **raw** market prices with band bounds. A rejected fee-adjusted shortcut passed all market outputs but skipped tiny exchanges that update oracle memory. Targeted boundary tests cover that regression; the retained implementation also matched the preceding candidate across 156 state cases and 2,436 compiled replay prefixes. These are bounded checks, not a proof of every possible Python input.

## Build declarations

`declarations/lending_amm.pxd` and `declarations/simulator.pxd` record the current native type declarations, including the explicit per-task fee argument. An external build package stages them alongside the matching `.py` files, then generates and compiles C++. They are review inputs here, not an automatically enabled build or a second accounting implementation.

The measured environment was CPython 3.11.15, Cython 3.2.4 and Apple Clang 21 with SDK 26.5. Compiler flags were `-std=c++17 -O3 -g0 -ffp-contract=off -fno-fast-math`. The [runnable kit and evidence](https://github.com/wavey0x/llamma-simulator_v2/tree/codex/native-benchmark-optimized/native-benchmark) remain separate from this source-review branch; the declaration files alone are not a complete reproduction package. Contributors can establish baselines on their own hardware.

Before an upstream submission, separate broadly useful Python changes from native-specific restrictions and simplify the visible accounting operations where possible. The measured gains justify continued work; custom storage and API changes still deserve explicit maintainer review.
