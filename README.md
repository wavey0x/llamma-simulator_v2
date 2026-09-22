# Curve LLAMMA Simulator

## Installation

### Locally using uv

```
pip install uv
uv venv
uv sync
```

### Recommended python version
It's recommended to use pypy to do faster simulations

```
uv python install pypy@3.11
uv venv --python pypy@3.11
uv sync -p pypy@3.11
```

## Running simulations

### Import data

Add pair to simulator/settings.py and import price data

```
python manage.py import_data {pair_name} (i.e. BTCUSDT)
```

### Performing calculations

_PDF will be added with more detailed explanation_

1) Change parameters in python manage.py for `calculate` command

2) Run simulations

```
python manage.py calculate
```
Results automatically will be saved in results folder.

### Separate scripts

Script ran for every pair is stored in `simulator/pairs` directory to save parameters used in calculations

```
export PYTHONPATH="${PYTHONPATH}:/path/to/your/directory"
python simulator/pairs/btcusd/calculate_a.py
```

### Market-specific studies

- [reUSD/sfrxUSD LP–crvUSD](simulator/pairs/reusd_sfrxusd_lp/README.md):
  onchain oracle reconstruction and joint v2 `A`/fee parameter screen.
