# reUSD/sfrxUSD LP / crvUSD parameter screen

This replay tests the proposed **three-leg USD oracle** against the market
price of LP collateral in crvUSD. It supplies the oracle's numeric output
directly to the unchanged `LendingAMM`, as in the proposed market wiring.
The USD aggregator is therefore part of the valuation policy, not a conversion
applied to both sides of the comparison.

## Reproduce offline

After `uv sync --frozen --python 3.11`, run from the repository root:

```bash
uv run --frozen python simulator/pairs/reusd_sfrxusd_lp/calculate.py \
  --history data/REUSD_SFRXUSD_LP/history.jsonl.gz \
  --output results/REUSD_SFRXUSD_LP/summary.json
```

The summary records the full configuration, input/code hashes, units, loan
starts and every A/fee evaluation. `--workers 1` runs serially. The committed
result uses CPython 3.11.15. There are two production scripts: collection and
calculation; replay needs no RPC connection.

## Oracle and market prices

```text
LP(p, vp, A) = StableSwap portfolio value(p, scaled A) × vp
reUSD feed = max(inverse bridge.price_oracle, 1 − baseRedemptionFee)

oracle = LP(pool.price_oracle, dampened virtual price, pool.A)
         × min(1, reUSD feed)
         × crvUSD_USD_aggregator.price()

spot = LP(pool.last_price, pool.virtual_price, pool.A)
       × inverse bridge.last_price
```

The final aggregator is
[`0x18672b1b0c623a30089A280Ed9256379fb0E4E62`](https://etherscan.io/address/0x18672b1b0c623a30089A280Ed9256379fb0E4E62#code).
Its historical `price()` is read at every sampled block and multiplied exactly
once, **after** the reUSD cap. The reUSD input is `priceAsCrvusd()`, not the
feed's `price()` endpoint, which already includes this aggregator.

This matches the [three-leg deployment composition](https://github.com/wavey0x/curve-stablecoin/blob/67b3b3b4057bf5d5f99b128b4b49cc9e2eae5f1a/scripts/deploy/llamalend/ethereum/markets/reUSDsfrxUSDLP-crvUSD/deploy.py#L373).
Its output has units **USD per LP**. The borrowing AMM consumes that raw number
as its oracle price; the independent market mark remains **crvUSD per LP**.
Multiplying spot by the aggregator too would instead simulate trading against
a USD numeraire and hide the oracle/market discrepancy caused by crvUSD's
USD price. This replay measures the specified wiring; it does not establish
that USD-denominated valuation is appropriate for a crvUSD borrowing market.

Products above omit explicit 1e18 divisions, which are applied after each leg
in the code to match `ChainOracle`. Pool A converts as `A_precise × 10000 / 200`.
The bridge price already incorporates the yield-bearing coin's underlying
conversion. Pool inputs incorporate sfrxUSD yield; no additional vault-rate
multiplier is applied.

The LP virtual-price EMA persists across the full history with an 866-second
time constant. Upward writes queue a new value; downward writes pass through
and reset the state. Read-only calls return `min(current VP, EMA)`. The base
replay assumes `price_w` at each sampled block. Existing pool EMAs and aggregator
values are historical reads, without another smoothing pass. The LP oracle
and its write schedule are counterfactual, not observed historical deployments.

## Frozen observations

The dataset contains **824,330 paired Ethereum end-of-block observations**,
from 20 March 2025 14:25:47 UTC to 30 August 2026 19:53:11 UTC. The final block
is 25,870,305, hash
`0x9667d83b47fc3a5dbffd2ad99db063cda226b43e7cd6423c139332ac0938f48b`.
Each row preserves block/hash/time, pool last prices and EMAs, LP virtual price
and A, the redemption handler and fee, the reUSD feed when deployed, and the
crvUSD/USD aggregator. Contract addresses are in the header; calls and scaling
are explicit in `collect_history.py`.

Sampling is every five blocks, plus every block in 24,340,000–24,380,000 and
25,740,000–25,785,000 around February and August stress periods. The largest
time gap elsewhere is 156 seconds. End-of-block reads omit intrablock ordering.
All 787,237 deployed reUSD feed observations match their reconstructed formula;
the 37,093 earlier rows retain a null feed and use historical dependencies.
Missing deployed reads, missing aggregator values and formula mismatches fail
collection rather than becoming default prices.

The schema-4 dataset enriches the previous frozen pool history. Its source hash
and fresh verification blocks are in `reused_history`. The aggregator, feed,
handler, fee and block hashes were freshly read for every observation. To
recollect without reuse:

```bash
# Set ETH_RPC_URL to an Ethereum archive endpoint in the environment.
uv run --frozen python simulator/pairs/reusd_sfrxusd_lp/collect_history.py \
  --end-block 25870305 \
  --dense-range 24340000:24380000 --dense-range 25740000:25785000 \
  --output data/REUSD_SFRXUSD_LP/history.jsonl.gz
```

Fresh collection has different provenance metadata, so its file hash can differ
while all paired readings match.

## Launch-period treatment

The first input block, 22,088,660, is the pool's first deposit/trading block,
20 March 2025 14:25:47 UTC. Supply was zero between deployment and that deposit.
The first 24 hours remain available to warm the EMA but cannot seed calibration
loans. The first eligible sampled start is block 22,095,830, 21 March 14:26:23 UTC.
This is an explicit startup assumption; genuine early observations are preserved.

A fresh every-block scan preserves 7,171 launch observations. With the full
USD oracle, the minimum gap between the supplied oracle value and market spot is
**-0.8658%**. The LP virtual price itself spans
1.000020–1.000303, consistent with early price/EMA settling rather than a
corrupt virtual-price spike.

For the current quick-screen case A=330 / fee 0.20%, adding all 1,434
original sampled launch starts gives a startup-only adjusted maximum of
**0.7600%**. Using every block and all 7,167 launch starts gives
**0.7643%**, versus **1.5049%** in the retained quick screen.
After the launch day, observations retain the five-block cadence. Denser
observations also change assumed oracle writes. Neither check changes this
case's historical maximum; they support retaining the stated launch policy.
Full-history price extrema in the summary still include warm-up rows.

## Replay and results

Each one-day loan starts with one collateral unit in four bands. At each
observation, the contemporaneous oracle is set before arbitrage against that
block's market spot, with 5 bp external execution cost. No candle extrema or
future observations enter the calculation. Initial and final crvUSD value use
`get_all_x()` with oracle memory initialized before the first valuation.

```text
band-adjusted loss = 1 − (1 − maximum raw loss)
                        × mean(((A − 1) / A) ** (band + 0.5))
```

The screen evaluates **246 deployable A/fee combinations** on 652 shared
one-day starts. Its coarse A grid spans 25–2,000 and fees span 0.05%–1.00%.
It independently refines A at every fee, rejecting combinations above the
deployment fee bound `min(4/A, 10%)`. The lowest tested adjusted loss is
**1.4554% at A=330 / fee 0.30%**.

At the base fee of 0.20%, its lowest tested adjusted loss is
**1.5049% at A=330**, with raw loss 0.9058%.
The independently selected A at each fee is:

| Fee | Best tested A | Band-adjusted loss |
| --- | ---: | ---: |
| 0.05% | 330 | 1.5547% |
| 0.10% | 215 | 1.5297% |
| 0.20% | 330 | 1.5049% |
| 0.30% | 330 | 1.4554% |
| 0.40% | 375 | 1.5029% |
| 0.50% | 790 | 1.7022% |
| 0.75% | 510 | 2.1707% |
| 1.00% | 205 | 2.3243% |

The base-fee case A=330 / fee 0.20% on **12,755 hourly/stress starts** gives
**1.5165%** adjusted loss. This denser schedule is a useful check on
the daily screen, not proof that every possible start has been tested.

The quick screen uses daily starts plus hourly starts before five separated
oracle/market dislocations. It refines A around each fee's lowest coarse loss,
then reports the best tested pair. This finite search is not a parameter recommendation or a
proved minimum discount. Evaluate an exact combination with
`--exact --a-values A --fees FEE`; use `--window-step-seconds 3600` for hourly
starts and `--windows-from RESULT.json` to hold starts fixed across scenarios.
Use `--loan-seconds` and `--bands` for duration and band-count sensitivity.
The summary preserves per-fee winners, tested deployment bounds and boundary
flags; an interior result still does not prove a global or future optimum.

Sensitivity checks use 626 identical starts shared with the five-block grid.
At A=330 / fee 0.20%, the adjusted maxima are:

| Change | Band-adjusted loss |
| --- | ---: |
| Base case | 1.5049% |
| Writes every 300 / 3,600 seconds | 1.5055% / 1.5048% |
| Initial VP −1% / +1%, after warm-up | 1.5049% / 1.5049% |
| Five-block sampling throughout | 1.5049% |
| External cost 0 / 10 / 50 bp | 1.5012% / 1.5272% / 2.0833% |

Sampling also changes assumed oracle writes. These are separate sensitivities,
not a combined stress envelope. Use the same starts with `--windows-from`
and vary `--oracle-update-seconds`, `--initial-vp-scale` or `--external-fee`.

## Validation and limits

Run `uv run --frozen python -m unittest discover -s tests -v`. Checks cover
floor/cap behavior, non-unit USD aggregator prices, independence of crvUSD spot,
EMA state, launch exclusion, chronology, sampling, normalization and parallel
reproducibility. A missing or nonpositive aggregator cannot silently become $1.

Optional compiled-contract checks use [StableSwapNGLPOracle and ChainOracle at
67b3b3b](https://github.com/wavey0x/curve-stablecoin/tree/67b3b3b4057bf5d5f99b128b4b49cc9e2eae5f1a),
curve-std `048cb23d0ed4c48768815ea5c46d4a676da8de35` and stableswap-ng
`7a9f6f11fb67e4778fb2496e65a09ec2939e339c`. Source/dependency hashes are checked.
The LP test compares 5,424 historical/synthetic observations across three write
cadences; maximum difference is 7 wei. The full three-leg multiplication is
also checked against compiled `ChainOracle` on 27 combinations, including
crvUSD prices of $0.90 and $1.10. Only EMA exponentiation approximates integer
contract math with floating point. Run with that checkout's locked environment:

```bash
REUSD_ORACLE_SOURCE=/path/to/curve-stablecoin/curve_stablecoin/price_oracles/v2/StableSwapNGLPOracle.vy \
  /path/to/curve-stablecoin/.venv/bin/python -m unittest discover -s tests -v
```

The redemption floor is a valuation rule, not guaranteed executable liquidity.
The replay excludes position-size exits, liquidity withdrawal, arbitrage
capital, gas/MEV, hard liquidation, final bad debt, issuer/redemption failure,
loan LTV, caps and monetary policy. Sampling, loan starts and the assumed oracle
write schedule remain limitations. Results generated before schema 4 used a
two-leg oracle without the USD aggregator and are superseded by this replay.
