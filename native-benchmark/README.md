# Native replay benchmark

- Close the remaining speed gap to Phil's C++.
- Keep Python as the single source of financial logic and preserve its accounting.
- Keep changes small, readable and suitable for Curve's maintainers to review.
- Use a lightweight, reusable build with deterministic outputs and proportionate checks.

This kit packages the builder, declarations, frozen inputs and benchmark driver alongside the optimized Python source.

## Starting points

- [Source review branch](https://github.com/wavey0x/llamma-simulator_v2/tree/codex/native-replay-review), pinned at `1505d10de6d74c13b58b16181bf8806f8c1d5ccd`.
- [Diff against Curve master](https://github.com/curvefi/llamma-simulator_v2/compare/master...wavey0x:codex/native-replay-review) and [readability/tradeoff review](../benchmarks/native/README.md).
- Curve baseline: `0bb370f02970c2c1056da8a4faffd0fa9be35575`; Phil baseline: `e9487b9fb5602f2a343f40276508966c1edb526d`.

The candidate already has typed numeric helpers, exact-input caches, numeric band storage and reusable batch state. There is no handwritten replacement of the accounting in C++. The build recipe reuses committed Python files, stages `.pxd` type declarations alongside them, and compiles them. The native declarations are model-specific: a changed field, signature or cache dependency may need review. Reusability does not mean arbitrary future commits will compile unchanged.

The [current results](optimized-results/README.md) show **52.6x the one-worker throughput of Curve Python**, with Phil still **1.99x faster**. The interpreted fork takes **44.7% less time** than Curve Python. The latest changes remove the global spot-price cache, reuse dimensionless geometry and avoid unnecessary band searches. Per-market timings, ordered outputs and build identities are included; [earlier handoff results](reference-results/README.md) remain available separately.

## Reproduce

**Use your own hardware and compiler.** Matching our machine, compiler or absolute timings is not required. Establish local baselines for every backend and compare optimizations on that same machine, keeping your chosen environment stable and recording it with the results.

The packaged scripts record our original environment and currently contain macOS-specific setup. Before running on another target, adapt the compiler/SDK handling in `cython/build.py` and `cython/setup.py`, Phil's shared-library command in `prepare.py`, and host reporting in `bench.py`; record your target in `cython/toolchain.json`. Install `uv` and use the dependency lock. This setup work should leave model logic and frozen benchmark inputs unchanged.

Clone the current kit and run from the repository root:

```sh
git clone --branch codex/native-benchmark-optimized \
  https://github.com/wavey0x/llamma-simulator_v2.git
cd llamma-simulator_v2
uv sync --frozen --project native-benchmark/cython
PY=native-benchmark/cython/.venv/bin/python

# Once per toolchain: unchanged Curve Python/Cython and pinned Phil C++.
$PY native-benchmark/prepare.py baseline --output build/benchmark-baseline

# Build the current source-review candidate, then run all five backends.
$PY native-benchmark/prepare.py candidate \
  --commit 1505d10de6d74c13b58b16181bf8806f8c1d5ccd \
  --output build/benchmark-001
$PY native-benchmark/bench.py run \
  --baseline build/benchmark-baseline --root build/benchmark-001 \
  --output build/results-001 --repeats 5
```

Both builds run the existing 48 short parity fixtures. The benchmark reuses their outputs and checks ordered losses, repeatability and one/four-worker agreement. Output directories are immutable: use new names for each experiment. The preparer builds **committed objects only**; uncommitted simulator edits are deliberately ignored. Use `--source /path/to/checkout` if the source is in another checkout.

For the next experiment, create your own branch, make one focused change and commit it. Run `$PY -m unittest discover -s tests`, prepare that full commit SHA into `build/benchmark-002`, and benchmark into `build/results-002`. The baseline can be reused while its toolchain stays unchanged. Changes to declarations or the builder also belong in the experiment commit, even though the builder reads them from the current checkout. Run with a clean tracked working tree so both are unambiguous.

The console ends with a timing table. Keep `summary.json` (medians/ranges), `comparison.json` (errors), `timings.json` (trials), `receipt.json` (identities), and the prepared build's `curve/release.json`. Avoid publishing local logs or entire build directories; they can contain local paths. Use `--reproducibility` on preparation when qualifying a release or changing the toolchain, rather than requiring two clean builds for every experiment.

## What this measures

Five interleaved trials, ETH/BTC/LP, 11,520 fixed independent windows, 518,400 replayed candles, 30/60-minute horizons and one/four process workers. Inputs and pools are resident; dispatch and ordered result return are timed. Compilation, imports, startup, preparation and warmup are excluded. Phil repeats each timed batch 32 times to stabilize its short timings. Totals sum the three market medians.

These are historical performance fixtures, not a new calibration: zero opening exchange memory, four bands, zero position shift, 0.0005 external execution fee and 0.25 dynamic-fee multiplier. The LP fixture retains its historical crvUSD spot / USD oracle denomination mismatch. [workload.json](inputs/workload.json) defines the actual tasks and hashes; [original-protocol.json](inputs/original-protocol.json) records the earlier sampling provenance, not an instruction to resample or use its earlier scoring step. Points have seven float64 columns: timestamp, open, high, low, close, volume, oracle. Tasks have eight: A, fee, start, end (exclusive), bands, external fee, dynamic multiplier, unused.

Keep the existing `5e-14` absolute loss tolerance; investigate any violation. The short state probes have a separate `1e-10` tolerance. Phil's older fee/oracle/recovery behavior differs in every window, so it is a **speed reference, not a correctness reference**. Do not change our accounting to match it. Frozen benchmark outputs provide the broad check; targeted tests cover changed state/cache behavior. Routine work needs no new parameter sweeps.

## Optimization priorities

1. Profile this current native build first; old profiles predate major fixes. Cython annotation HTML is generated beside the staged source under `build/benchmark-001/curve/candidate/` and helps locate remaining Python operations.
2. Investigate expensive repeated math, Python/native call boundaries, allocation and band traversal, guided by the measured profile. Change one cause at a time and retain a before/after table for all three markets and both worker counts.
3. Prefer small declarations, local reuse and obvious invariants. New cache state or custom storage needs a clear whole-workload win (roughly 10–15% is a useful bar), readable dependencies and targeted reset/invalidation tests.
4. Report interpreted Python performance as well as compiled performance. The current fork improves both; preserve that advantage and keep looking for ways to simplify `.read()`/`.write()` storage operations.

Do not add a second financial implementation, source-rewriting framework, fast-math, reassociated equations, reduced precision or looser tolerances. Preserve fee charging, oracle write timing, zero-input behavior, replay order and recovery accounting. Avoid removing runtime `pow` merely because a rewrite looks algebraically equivalent: that previously broke numerical parity. Generic Python LRU caches, ordinary dictionary band storage, extra small caches, merging compilation units and aggressive compiler flags were previously unhelpful; revisit only with new evidence.

Keep empty-band prechecks on raw prices. A fee-adjusted shortcut passed the market benchmark but skipped tiny exchanges that write oracle memory; `tests/test_empty_band_quotes.py` covers the regression. Targeted checks should follow the behavior being changed, rather than adding fresh parameter sweeps.

Return a focused commit/diff, per-market before/after timings, maximum error and a sentence on readability/API costs. Work on your own branch; leave `codex/native-replay-review` unchanged. The combined candidate is an experiment, not blanket approval for upstreaming all of its storage, cache and API tradeoffs.

Third-party headers under `vendor/` are the frozen nlohmann/json 3.12.0 headers used by Phil's CLI, with their [MIT license](vendor/LICENSE.nlohmann). Phil's model is fetched at its pinned public commit. `driver.cpp` only adapts resident inputs to its existing `simulate()` function.
