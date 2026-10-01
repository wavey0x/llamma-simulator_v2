# Native replay benchmark handoff

Close the remaining speed gap to Phil's C++ while keeping Python as the single source of financial logic and keeping changes small enough for Curve's maintainers to review. This kit packages the existing builder, declarations, frozen inputs and benchmark driver; it changes no simulator formulas.

## Starting points

- [Source review branch](https://github.com/wavey0x/llamma-simulator_v2/tree/codex/native-replay-review), pinned at `3f2f95d1e1528cc17bcd7b2454c7c76b28c0fd18`.
- [Diff against Curve master](https://github.com/curvefi/llamma-simulator_v2/compare/master...wavey0x:codex/native-replay-review) and [readability/tradeoff review](../benchmarks/native/README.md).
- Curve baseline: `0bb370f02970c2c1056da8a4faffd0fa9be35575`; Phil baseline: `e9487b9fb5602f2a343f40276508966c1edb526d`.

The candidate already has typed numeric helpers, exact-input caches, numeric band storage and reusable batch state. There is no handwritten replacement of the accounting in C++. The build recipe reuses committed Python files, stages `.pxd` type declarations alongside them, and compiles them. The native declarations are model-specific: a changed field, signature or cache dependency may need review. Reusability does not mean arbitrary future commits will compile unchanged.

The [fresh handoff verification](reference-results/README.md) reproduces the earlier loss arrays exactly, with about 27x the one-worker throughput of Curve Python and a remaining 3.8x gap to Phil. It includes per-market timings, ordered outputs and build identities.

## Reproduce

**Verified target: Apple Silicon macOS, Python 3.11.15, Cython 3.2.4, Apple Clang 21.0.0 (`clang-2100.1.1.101`), SDK 26.5.** See [toolchain.json](cython/toolchain.json) for the exact compiler target string and flags. Install `uv` and matching Xcode command-line tools first. Other systems require an explicit port of the compiler/SDK and shared-library commands; changing the target JSON alone is insufficient. Measure every backend on the same machine. Absolute times from different hosts are not comparable.

From the repository root, after cloning the handoff branch:

```sh
uv sync --frozen --project native-benchmark/cython
PY=native-benchmark/cython/.venv/bin/python

# Once per toolchain: unchanged Curve Python/Cython and pinned Phil C++.
$PY native-benchmark/prepare.py baseline --output build/benchmark-baseline

# Build the current source-review candidate, then run all five backends.
$PY native-benchmark/prepare.py candidate \
  --commit 3f2f95d1e1528cc17bcd7b2454c7c76b28c0fd18 \
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
4. Report interpreted Python performance as well as compiled performance. The existing fork was roughly 10% slower when interpreted; improving that and simplifying `.read()`/`.write()` storage operations would strengthen an upstream proposal.

Do not add a second financial implementation, source-rewriting framework, fast-math, reassociated equations, reduced precision or looser tolerances. Preserve fee charging, oracle write timing, zero-input behavior, replay order and recovery accounting. Avoid removing runtime `pow` merely because a rewrite looks algebraically equivalent: that previously broke numerical parity. Generic Python LRU caches, ordinary dictionary band storage, extra small caches, merging compilation units and aggressive compiler flags were previously unhelpful; revisit only with new evidence.

Return a focused commit/diff, per-market before/after timings, maximum error and a sentence on readability/API costs. Work on your own branch; leave `codex/native-replay-review` unchanged. The combined candidate is an experiment, not blanket approval for upstreaming all of its storage, cache and API tradeoffs.

Third-party headers under `vendor/` are the frozen nlohmann/json 3.12.0 headers used by Phil's CLI, with their [MIT license](vendor/LICENSE.nlohmann). Phil's model is fetched at its pinned public commit. `driver.cpp` only adapts resident inputs to its existing `simulate()` function.
