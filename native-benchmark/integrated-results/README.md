# Integrated native replay qualification

The integrated candidate fixes the timestamp regression, preserves optional oracle values, and combines the selected storage/math changes with deterministic PGO. Python remains the authoritative accounting implementation. It reduces native replay time by **44.8%** against our previous candidate and reaches approximately Phil's speed on the existing workload.

## Paired comparison

Seven interleaved trials of five batches; one worker, all 11,520 frozen ETH/BTC/LP replays per batch. The previous candidate is `7065240`; the coworker proposal is `0602334`; the integrated candidate is `37b552e400434cec2a6c10795f6fbb30c2b11e2f`.

| Native implementation | Median |
|---|---:|
| Previous candidate | 108.716 ms |
| Coworker proposal with PGO | 64.948 ms |
| Integrated candidate with PGO | **60.050 ms** |
| Phil C++ | 61.927 ms |

The integrated candidate takes 7.5% less time than the proposal. A separate three-trial interpreted-Python comparison measured 3.077574 → 2.891795 seconds, **6.0% less time**. Raw [native trials](paired/trials.json) and [Python trials](paired-python/trials.json) accompany their summaries. Compare within each session; absolute times from earlier qualifications are not an incremental measurement.

## Common benchmark

Five interleaved trials; resident inputs, dispatch and ordered return included; preparation, startup and warmup excluded. Totals sum the three market medians. The [protocol](../README.md#what-this-measures) records the historical LP denomination mismatch and remaining fixed assumptions; no new calibration or sampling was performed.

| Backend | One worker | Four workers |
|---|---:|---:|
| Curve Python | 6.494461 s | 1.764144 s |
| Unchanged-source Cython | 2.404675 s | 0.676948 s |
| Fork Python | 2.932965 s | 0.812735 s |
| Fork Cython with PGO | **0.060101 s** | **0.019974 s** |
| Phil C++ with older accounting | 0.062182 s | 0.018946 s |

Native replay is 108.1 times Curve Python throughput on this host. It takes about 3% less time than Phil with one worker and 5% more with four: approximately equal performance, not a decisive lead. Phil's older accounting differs in every window and is not our correctness reference. These measurements do not establish end-to-end application performance.

[summary.json](summary.json) contains per-market medians/ranges; [timings.json](timings.json) contains all trials; [comparison.json](comparison.json) contains ordered loss errors. [receipt.json](receipt.json) identifies the builds, protocol and output hashes. All ordered arrays are included.

## Verification

- All 30 output arrays are byte-identical to our previous qualified run. Fork Python matches Curve Python exactly; native maximum absolute loss error remains `4.007905118896815e-14`, under the unchanged `5e-14` limit. Repeated and one/four-worker outputs are bitwise equal within each backend.
- All 57 Python tests, 14 focused compiled tests and the pipeline's 48 short replay/state/error fixtures passed. State traces match the previous candidate across 156 Python cases and 2,436 compiled replay prefixes.
- Regression tests cover omitted timestamps, explicit infinities/NaN, backwards time, rejection before mutation, single/batch replay, unused input rows, optional properties, raw-price fallback, storage deletion/reset/overflow and geometry invalidation.
- Two clean builds have identical generated C++, merged PGO profiles and extension binaries. [release.json](qualification/release.json) and the [first build receipt](qualification/candidate/build.json) record the evidence; the [second build](qualification/rebuild/build.json) is independently generated. The [short validation receipt](qualification/validation/verification.json) records its scope and tolerances.
- A deliberately mismatched profiling-tool pin was rejected. Disabling PGO remains a profile choice; the [ordinary build passed](without-pgo/release.json) without a second source implementation.
- The source-only review commit `72bb07ebc7d12d8465fd3e0c5e546d19d2378012` rebuilt through the same unmodified kit and passed the short gate. Its generated C++, PGO profile and binaries are identical to the timed candidate; see the [source-only receipt](source-only/release.json). Code and review declarations match byte for byte.

The qualified artifact hash is `12683bc9c9a3328a13612e6a740137f8d00969f6b8a4211c8811c64c27d91c18`; build ID `9745f583fded7b3e885edaf520ce02d7a3263d051f4829a96ebb1dad2fb04981`. The pinned environment uses CPython 3.11.15, Cython 3.2.4, Apple Clang 21 and SDK 26.5. The strict floating-point flags remain unchanged. Compiler training uses the fixed synthetic recipe; its source hash, matching `llvm-profdata` version/binary hash and generated profile hash are recorded. Determinism is qualified within this environment, not promised across different compilers or hosts.

## Source organization

The integration has focused commits for band storage/cache keys, ordinary power syntax, oracle representation/validation, and the PGO builder. Our shared-input solver and conservative raw-price guards remain intact. The full campaign is eight production Python lines smaller than the original review candidate, while adding targeted tests. Custom band storage and native type restrictions still deserve maintainer review; see the [readability assessment](../../benchmarks/native/README.md).

The public interface retains `None` timestamps and `raw_p_oracle=None`. Explicit nonfinite observations are rejected before mutation in both direct and batch paths. Timestamp properties and quotes now reject nonfinite values explicitly as well. The internal missing marker cannot be supplied through those public adapters. Private numeric methods remain implementation details.

The source-only review branch is `codex/native-replay-integrated`; the build/evidence branch is `codex/native-search-integrated`. Both are separate from the previously published branches. Matching declarations live beside the source review and in the runnable kit. Publication and application-engine promotion are separate actions.
