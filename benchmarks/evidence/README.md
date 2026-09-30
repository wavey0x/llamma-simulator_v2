# PR #11 replay evidence

This branch holds reproduction artifacts, separately from the upstream patch.
All benchmarks used CPython 3.11.15 on macOS 26.6.2 arm64. Timings are workload-
and machine-dependent, exclude collection/preparation, and use alternating
execution order after two warmup batches per revision.

| Workload | Upstream | Previous PR | Candidate | Upstream / candidate | Previous PR / candidate |
| --- | ---: | ---: | ---: | ---: | ---: |
| 2,000 synthetic windows, 7 trials | 4.029 s | 2.273 s | 1.415 s | 2.85x | 1.61x |
| 4,000 historical windows, 3 trials | 7.262 s | 4.186 s | 2.502 s | 2.90x | 1.67x |

Every ordered loss matched bit for bit across all three revisions, including
warmups and every timed trial. Each result JSON contains the common full loss
array, its hash, every timing, effective settings, and executed source hashes.
`pins.json` was saved before measurement:

- Upstream: `f18e1231bdc47798314463d1595c69b9dcaacd6f`
- Previous PR: `e94f9d196750aba623d2b8bd3a3954e8b5abb65c`
- Candidate: `debea182cbcd639af4c5d3089b8e8e877f5dbb2a`

Run from this branch's repository root; the benchmark and randomized checks
require only the Python standard library:

```sh
python benchmarks/benchmark_amm.py \
  --baseline f18e1231bdc47798314463d1595c69b9dcaacd6f e94f9d196750aba623d2b8bd3a3954e8b5abb65c \
  --candidate debea182cbcd639af4c5d3089b8e8e877f5dbb2a \
  --windows 2000 --warmup 2 --repeats 7 --output synthetic.json

python benchmarks/benchmark_amm.py \
  --baseline f18e1231bdc47798314463d1595c69b9dcaacd6f e94f9d196750aba623d2b8bd3a3954e8b5abb65c \
  --candidate debea182cbcd639af4c5d3089b8e8e877f5dbb2a \
  --workload benchmarks/evidence/historical-workload.json.gz \
  --warmup 2 --repeats 3 --output historical.json

python benchmarks/evidence/verify.py \
  --candidate debea182cbcd639af4c5d3089b8e8e877f5dbb2a --output correctness.json
```

The historical input file embeds every candle and oracle value actually
replayed, along with original window indices/order, source-file hashes and
collection/preparation provenance. No access to the original dataset or RPC
endpoint is needed. Compressed file SHA-256:
`d78aaccca01c314179d76bb2e93bdb911650a003af62f3623a2b6c61dc4938eb`.

These are LP observations with **upstream replay initialization**: first-candle
open determines liquidity placement, and the constructor seeds oracle memory.
The prepared oracle-state array is not loaded. This comparison does not validate
the application's causal initialization. Spot is crvUSD/LP and oracle is USD/LP,
preserving the recorded observation convention; this is a performance/parity
fixture, not a market calibration or an endorsement of that denomination choice.

`verify.py` preserves the prior review's fixed randomized scenarios while loading
the three actual revisions. All 1,500 valuation states and 1,000 replay cases
matched, including complete logs, oracle state and nonzero balances. There were
981 successful replays and 19 matching pre-existing band-limit assertion failures.
Dictionary keys containing unused zeros are intentionally excluded from state
comparison. This assertion-based research script rejects `-O` explicitly.

With project dependencies installed, validation commands were:

```sh
python -m unittest discover -s tests -v
python -O -m unittest discover -s tests -p test_benchmark_amm.py -v
```

All 12 tests passed normally, and all four benchmark tests passed under `-O`.
Those optimized-mode tests verify benchmark checks and unconditional warmups,
not preservation of assertion-based simulator failure conditions. Failure tests
cover reference, warmup and timed stages, both receipt creation and overwrite,
nonfinite values, differing outputs, missing outputs and replay exceptions.

Previously observed PyPy JIT repeatability failures occurred even on unchanged
upstream source. No PyPy JIT correctness or speedup claim is made here.
