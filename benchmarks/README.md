# AMM replay benchmark

From the repository root:

```sh
python benchmarks/benchmark_amm.py \
  --baseline f18e1231bdc47798314463d1595c69b9dcaacd6f \
  --windows 2000 --warmup 2 --repeats 7
```

This uses only the standard library. It executes each revision's `LendingAMM`,
liquidity class and existing `Simulator.calculate_loss` method, extracting the
method to avoid importing unrelated data providers. Financial calculations are
not copied into the benchmark. By default the candidate is the working tree;
use `--candidate COMMIT` to pin it. `--baseline` accepts multiple revisions for
a comparison against both upstream and an earlier patch.

The fixed-seed workload combines A=10/100/300/600, fees=0.001/0.005, 30/60-minute
windows, four bands, a 0.25 dynamic multiplier and a 0.0005 external fee.
Logging and verbose output are disabled. Trials alternate execution order and
require every ordered loss to match bit for bit, including during warmup. Output
includes resolved revisions, executed source hashes, settings, workload hash,
all timings and the complete common loss array; `--output result.json` saves it.
Collection, preparation, multiprocessing and aggregation are outside this
measurement. Speedups depend on the workload and machine.

Warmups run unconditionally. Explicit exceptions reject nonfinite values or
unequal outputs before any receipt is written. Tests cover failures during
reference generation, warmup and timed trials, including preservation of an
existing receipt:

```sh
python -m unittest discover -s tests -v
python -O -m unittest discover -s tests -p test_benchmark_amm.py -v
```

The optimized-mode tests establish that benchmark validation remains active;
they do not establish preservation of the simulator's assertion-based failure
conditions under `-O`.

On macOS arm64, CPython 3.11.15, after two warmup batches per revision:

| Workload | Upstream | Previous PR | Updated PR | Upstream speedup | Additional speedup |
| --- | ---: | ---: | ---: | ---: | ---: |
| 2,000 synthetic windows, median of 7 trials | 4.029 s | 2.273 s | 1.415 s | 2.85× | 1.61× |
| 4,000 historical windows, median of 3 trials | 7.262 s | 4.186 s | 2.502 s | 2.90× | 1.67× |

The measured revisions are upstream `f18e1231bdc47798314463d1595c69b9dcaacd6f`,
previous PR `e94f9d196750aba623d2b8bd3a3954e8b5abb65c` and updated runtime
`debea182cbcd639af4c5d3089b8e8e877f5dbb2a`. Ordered losses match exactly across
all three. [Pinned evidence and reproduction commands](https://github.com/wavey0x/llamma-simulator_v2/tree/ac5f0b03ab7305ccd46e04b72d51972a4362c2e7/benchmarks/evidence)
include every timing, full loss arrays, randomized checks and exact historical
inputs. The data lives on a separate evidence branch, outside this patch.

Historical replay uses LP candles with **upstream initialization**, not the
application's causal initialization. Its compressed workload embeds each exact
window and its provenance; no access to the original dataset is required.
Pass it as `--workload historical-workload.json.gz` instead of `--windows`.
Workload JSON contains `tasks` as `[A, fee, rows]` entries (rows contain timestamp,
OHLC, volume and oracle), plus a `provenance` object. The settings above remain
fixed. Input loading is outside the timer.

The runtime skips empty-band price calculations, visits only existing band keys
during total valuation, avoids disabled logging work and removes an unused fee
collection. Valuation still includes manually funded bands and uses ascending
order within `[-500, 500)`. Numerical results, oracle state and nonzero balances
are preserved; dictionary key sets may change because unused zero entries are
no longer created. All 12 tests pass; randomized checks also matched 1,500
valuation states and 1,000 replays, including 19 pre-existing band-limit failures.

PyPy 7.3.21 on this host failed repeatability with the unchanged baseline alone:
one of 800 losses changed by 0.0004841334007219533 between repeated replays.
The benchmark intentionally fails rather than relaxing equality. Before/after
losses matched exactly with `pypy3.11 --jit off`; no PyPy JIT speedup or parity
claim is made here.
