# Instructions for anyone who clones this repo

This repo analyses single-name CDS moves inside the CDX / iTraxx indices (spread, upfront, all_upfront, risky_pv01 changes between two dates, dispersion, top-N expected-loss share).
You only need to **use** the data. The data is built and published by one person (Matt Moore) and lives on the shared drive; it is not in GitHub.

## What you need
- Access to the shared drive `S:\Structured Credit\Matt Stuff\tranche_single_name_moves\` (the published data is in its `data\` folder, about 145 MB, read only for you).
- Python 3.12+ with `pandas`, `numpy`, `pyarrow`, `ipykernel` (plus Jupyter or VS Code to open notebooks). Nothing else: no Bloomberg, Databricks, Quantifi or Excel.

## Set up
```
git clone https://github.com/mmoorejmm17/tranche_single_name_moves.git
cd tranche_single_name_moves
python -m venv .venv
.venv\Scripts\activate
pip install pandas numpy pyarrow ipykernel
```
(Do not run `poetry install`: it also pulls the Bloomberg / Databricks packages that only the data owner needs.)

## Use it
1. Open a notebook in `notebooks/` and select the `.venv` kernel:
   - `index_spread_moves.ipynb`: pick an index (e.g. `HY47`) and two dates; shows which names are missing data, then every name's spread, upfront, all_upfront and risky_pv01 at both dates and the change, plus index metrics.
   - `dispersion.ipynb`: dispersion of all_upfront across an index over time.
   - `topN.ipynb`: share of the index's expected loss in the N widest names, two dates.
2. Change the parameters in the notebook's parameter cell (INDEX, dates, N ...) and Run All.
3. The first cell prints which snapshot you are reading, e.g. `data published 2026-10-26 08:34; prices through 2026-10-23`.

## Where the data is read from
The code looks, in order, at: the `CDS_DATA_DIR` environment variable; this clone's own `data\` folder (if it has a `MANIFEST.json`); then the shared-drive snapshot
`S:\Structured Credit\Matt Stuff\tranche_single_name_moves\data`. A fresh clone has no `data\`, so it reads the shared drive automatically. If the drive is mapped to a different letter, or you want a
local copy, copy that `data\` folder into the clone, or set `CDS_DATA_DIR` to it. Never edit files in `data\`.

## Ground rules
- `analysis/` only reads data. It never writes, so you cannot damage the data store by running notebooks.
- `pricing/` builds and publishes the data (Bloomberg, Databricks, Quantifi, Excel add-in). Please do not run it for now; the owner is still fixing old missing data. Running `update_store` yourself may be allowed later.
- The data can change when the owner re-publishes. If numbers shift, compare the "data published" line at the top of the notebook.
- Missing / approximated data is flagged in the notebook (red = no price, amber = wide name at a marked-down recovery, blue = re-priced from the other coupon, purple = Markit add-in).
  `CLAUDE.md` has the definitions (what "priced" means, how all_upfront, dispersion and top-N are computed).

## Folders
`analysis/` code that reads data, `notebooks/` the three notebooks, `data/` the published snapshot (not in git), `pricing/` owner-only code, `config/` hand-maintained inputs, `docs/` Markit user guide.
