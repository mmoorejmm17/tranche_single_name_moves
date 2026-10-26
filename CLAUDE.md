# tranche_single_name_moves

## Project layout (pricing / analysis split, 2026-10-26)
Writing the data and using it are separate exercises. The data is ONE standalone published store (`data/base`) that only the owner (Matt) writes, by running `pricing/shared/publish.py`; everyone else only READS it.
```
CLAUDE.md, pyproject.toml, poetry.lock, poetry.toml, .gitignore (cache/, data/, __pycache__, .venv, *.parquet)
analysis/   READ side. cdx_spreads (spread_changes, priced_mask, show_table, reports), index_metrics (dispersion / top-N), charts. Reads ONLY data/base; needs just pandas/pyarrow/numpy
            (no Bloomberg, Databricks, Quantifi or Excel); never imports pricing/ and never writes anything.
pricing/    WRITE side, owner only. Three folders:
              shared/       building blocks both workflows use: _paths (imported first; points cdx_spreads at the working store cache/base and config/), cdx_store (rosters, maturities, update_store),
                            cdx_members, cds_pricer + cds_recalib (Quantifi), markit_addin (Excel add-in), publish (cache/base -> data/base).
              daily/        update.py: bring in NEW pricing as it becomes available (Bloomberg + Databricks).
              fix_history/  repair OLD missing data: gaps.py (list every gap), overrides.py (price the ballpark conv_spreads in config/overrides.csv), addin_fill.py (Markit add-in fill), defaulted.py.
notebooks/  index_spread_moves, dispersion, topN (analysis only; first cell prints which snapshot they read).
config/     hand-maintained inputs, read by pricing and copied into the store's reference/ on publish: cds_index.csv (the 40 indices), ticker_aliases.csv, ticker_ids.csv, overrides.csv, override_series/.
data/       PUBLISHED stores (gitignored). A STORE is a folder with a fixed layout, and the code is pointed at one store folder (env CDS_STORE, default `base`); today only data/base exists:
              data/base/prices/     spreads.parquet (raw Databricks mirror), priced_at_index_coupon, addin_fill, addin_fill_defaulted, overrides_priced, recalibrated
              data/base/reference/  index_maturity.csv, cds_index.csv, overrides.csv, credit_events, defaulted_prices, members/ (one roster csv per index)
              data/base/metrics/    index_metrics.parquet
              data/base/MANIFEST.json   (store, published_at, store_through)
            Nobody edits it by hand. A future `live` store (dates after a cutoff) would use the same layout next to it.
cache/      the pricing WORKING area (gitignored); analysis never reads it:
              cache/base/   working copy of store `base`, same layout as data/base (what the pricing scripts write; publish copies it to data/base)
              cache/review/ review sheets per name (SIRI.xlsx ...), gaps.csv, old/ archive
              cache/work/   scratch: addin_raw/, tenor_map.parquet, addin_unfilled, addin_fill_errors, priced_parts/ (resumable pricer parts), backups/, proposed_ticker_changes.csv
docs/       markit_excel_guide/ (Markit add-in user guide and data dictionary).
archive/    unmapped_members.xlsx (early mapping work, unused by the code).
```
Workflow (owner), all writing to cache/base (and cache/work) and ending with publish:
- NEW pricing (daily): `python pricing/daily/update.py` -> `python pricing/shared/cds_pricer.py` (system Python) -> `python pricing/shared/publish.py`.
- REPAIR old missing data (fix_history), the standard process, one name at a time (system Python: it has Bloomberg, Quantifi and openpyxl):
  1. `python pricing/fix_history/review.py list` ranks the names with gaps (built on `gaps.py`, which also writes `cache/review/gaps.csv` with every gap run and its neighbouring prices).
  2. `python pricing/fix_history/review.py make SIRI` writes `cache/review/SIRI.xlsx`: sheet `review` has one row per missing date per index with years_left, bbg_5y (Bloomberg 5Y PX_LAST; ticker from
     BBG_5Y_CDS_Ticker in the mappings file), best_guess (+ guess_method) and the yellow `override_spread` column that is the only input (blank = stays a gap; copy/paste best_guess where you agree);
     `runs` shows the gaps, the neighbouring Markit prices, the ratio fit and its backtest; `charts` plots bbg / guess / override per index. Re-running make keeps what you entered (old file archived in cache/review/old).
     Guess = bbg 5Y x ratio(T), where ratio = this name's Markit conv / bbg 5Y at the same years-to-maturity on the overlap dates (guess.py; SIRI backtest mean abs error 5.8 bp), else a straight line between the
     neighbouring Markit prices, else flat. It is an estimate; SIRI worked because Markit has the name from 2024-11-26 to learn the ratio from.
  3. Fill `override_spread`, save, close, then `python pricing/fix_history/review.py ingest SIRI`: rewrites that name's rows in `config/overrides.csv` (tracked in git; the sheet is the single record for the name),
     writes `config/override_series/SIRI_review.csv` (one conv_spread per day) and prices every approved cell with Quantifi into `cache/overrides_priced.parquet` (par_spread, risky_pv01, upfront, all_upfront
     derived from the conv_spread at the index coupon, flat curve). Un-approving a cell (blank it) and re-ingesting undoes it: nothing underneath is ever changed, the overlay just wins.
     (`overrides.py` is the engine and also accepts hand-written rows: index_name [blank = all], ticker, start_date, end_date, conv_spread, conv_spread_end [ramp], series, force, source, note.)
  4. `python pricing/shared/publish.py`. Overridden cells are state `override` (teal) in the notebooks and their own bucket in `priced_counts` / `pricing_coverage`; they feed index_metrics.
  `addin_fill.py pull|assemble` and `defaulted.py` are the other fix_history steps (see below). Waterfall in the analysis: override > Databricks > Markit add-in.
PLANNED (agreed 2026-10-26, not built): a base / live split. `base` = the frozen history up to a cutoff date, changed only through fix_history; `live` = dates after the cutoff, appended by daily/update.py,
which then cannot touch old dates. Analysis would read base + live together.
Others running update_store is a possible future option, not a goal while the old missing data is being fixed. The pricing steps need the SYSTEM Python (Quantifi Toolkit, Excel); publish and the notebooks run in the project .venv.
Where this file says `src/` or `cache/` below it predates the split: scripts are now in `pricing/`, readers in `analysis/`, and the notebooks read `data/`.

## Definition: PRICED (revised 2026-10-25; supersedes the earlier 5-stat version)
A single-name CDS curve point (name, date, maturity) is **priced** if it has EITHER
- (1) `par_spread` AND `risky_pv01`  (coupon-free), OR
- (2) `conv_spread` OR `upfront`, AND `risky_pv01`, AND the `running_coupon` that quote is at.
Everything else is derived. Identities (verified against Markit add-in data at both coupons, max error 0.009 pts): `all_upfront = par_spread x risky_pv01 / 100` and
`upfront(C) = (par_spread - C) x risky_pv01 / 100` for ANY coupon C (spreads in bp, upfront in points), so (par_spread, risky_pv01) is a complete description and
switching coupon is arithmetic; going the other way `par_spread = C + upfront x 100 / risky_pv01`. `conv_spread` at a coupon C is the flat-spread equivalent of
`upfront(C)` (what you plug into CDSW to get that upfront), so it needs the Quantifi conversion and it DEPENDS on the coupon (ACI: 194.1 bp at 500, 204.2 at 100).
`conv_spread == 0` is Markit's 'no spread solves' placeholder, not a price. Wide names (CSC, Hertz, Xerox ...) therefore ARE priced (they have par_spread and
risky_pv01); only their conv_spread is undefined at 40% recovery (see `cds_recalib.py`).
**The goal is for every name on every date to be priced.** Plan: a Quantifi-based pricer per curve point that calibrates from whatever we have and re-prices at any
coupon, with the curve assumed flat across tenors at first. Finding so far: calibrating a flat curve from conv_spread alone does NOT reproduce Markit's other-coupon
quotes (upfront off by up to 0.39 pts, conv by up to 11 bp) because conv_spread is not coupon-independent, so keep Markit's par_spread + risky_pv01 whenever we have them.
CODE STATUS: implemented. `cdx_spreads.priced_mask()` / `pricing_coverage()` / the notebook use this definition, and `cds_pricer.is_priced()` mirrors it (the old 5-stat `PRICED_STATS` is gone).
Status 2026-10-25: 2,362,483 of 2,549,875 index-member-date cells priced (92.7%), incl. 4,876 wide-name rows with no conv_spread; 79.2% are quoted at the index's own coupon (the rest need re-pricing); unpriced = 187,107 no row + 285 stubs.
A manual override currently fills only conv_spread; to make a row priced it will need (par_spread, risky_pv01) or (upfront/conv_spread, coupon, risky_pv01).

## Data: single-name CDS (Databricks)

Single-name CDS data lives in `teams.structured_credit_abhutra.cds_transform_interpolated`
(Delta, managed; owner mmoore@hudsonbaycapital.com, schema belongs to abhutra).

### Connection / auth
- Workspace: `https://adb-2702578869083687.7.azuredatabricks.net` (workspace_id 2702578869083687)
- Auth: Databricks CLI OAuth (SSO), profile `mmoore@hudsonbaycapital.com` in `~/.databrickscfg`
  (`auth_type = databricks-cli`). No tokens are stored; nothing secret goes in this repo.
- If you see "refresh token is invalid", the login expired. The user must run (browser flow):
  `! databricks auth login --profile mmoore@hudsonbaycapital.com`
- CLI is on PATH (`databricks`). Always pass `-p "mmoore@hudsonbaycapital.com"`.
- SQL warehouse (default): `1c781003e5fc2571` (Serverless SQL Warehouse, Large).
  HTTP path: `/sql/1.0/warehouses/1c781003e5fc2571`. Smaller serverless options:
  `41be5e38d1608ab9` (Small), `59c9bd01752f30ca` (2X-Small).

### Running queries
- Python: `databricks-sql-connector` or `databricks-sdk` using the profile above
  (e.g. `WorkspaceClient(profile="mmoore@hudsonbaycapital.com")`).
- CLI ad hoc (Git Bash needs `MSYS_NO_PATHCONV=1` or `/api/...` gets mangled into a Windows path):
  `MSYS_NO_PATHCONV=1 databricks api post /api/2.0/sql/statements --json @q.json -p "mmoore@hudsonbaycapital.com"`
  with `q.json` = `{"warehouse_id":"1c781003e5fc2571","wait_timeout":"30s","statement":"SELECT ..."}`.
- Schema lookup: `databricks tables get teams.structured_credit_abhutra.cds_transform_interpolated -p ...`

### Table facts (checked 2026-10-23)
- ~10.07M rows; 500 tickers (`market_cds_ticker`); `published_date` 2022-04-01 to 2026-10-23.
- Partitioned by `market_cds_ticker`: filter on it (and on `published_date`) to keep queries cheap.
- Grain: one row per `published_date` x `market_cds_ticker` x `tenor` (and curve variants; check
  `doc_clause`, `currency`, `batch` before assuming uniqueness).
- Tenors: 6M, 1Y, 1Y6M, 2Y, 2Y6M, 3Y, 3Y6M, 4Y, 4Y6M, 5Y, 5Y6M, 6Y, 6Y6M, 7Y, 7Y6M, 8Y, 8Y6M, 9Y, 9Y6M, 10Y
  (`tenor_order` gives numeric sort).
- `doc_clause`: MM14, XR14, CR14. `currency`: USD, EUR, JPY. `primary_price_type`: ConvSpread, Upfront.
- `batch`: `EOD` vs `EOD-Interp`. Interpolated rows (non-quoted tenors) are built from the neighbouring
  quoted tenors: see `prev_tenor`/`next_tenor`, `prev_weight`/`next_weight`, and the `*_derived` columns.
- Key columns:
  - Identity: `market_cds_ticker`, `markit_ticker`, `markit_curve_id`, `markit_id`, `red_code`, `short_name`
  - Static: `sector`, `region`, `country_territory_code`, `implied_rating`, `tier`, `cds_index`
  - Dates: `published_date`, `cds_maturity`, `imm_date_5y`
  - Quotes: `par_spread`, `conv_spread`, `upfront`, `all_upfront`, `running_coupon`, `primary_coupon`
  - Risk: `credit_dv01`, `risky_pv01`, `ir_dv01`, `rec_01`, `default_probability`, `cds_assumed_recovery`
- Confirmed with user: `conv_spread` ("conventional", i.e. the quoted spread at the 100/500 coupon) is the standard metric,
  and spreads are in bp (e.g. 200.7). `par_spread` is the coupon-free par spread, not what trades.
- TODO (confirm with user): whether to prefer `*_derived` columns over the raw ones for interpolated rows.

### Upstream source: `vendor_data.markit.composite_cds_pricing_silver` (checked 2026-10-25)
- Raw Markit composite pricing that `cds_transform_interpolated` is built from. Not partitioned; keyed by `ticker`
  (= Markit ticker, e.g. ALBERCO), `index_tenor`, `doc_clause`, `currency`, `running_coupon` (decimal there: 0.05, not 500), `batch`
  (many intraday batches per day, e.g. N-1530, L-1700, J-1600), `published_date`.
- Known gap: Albertsons Cos Inc (Market_CDS_Ticker `ACI`, Markit `ALBERCO`) has no usable prices at all.
  - Source table: `ALBERCO` exists only from 2026-09-24 (22 days, 10,424 rows). Every row has par_spread = conv_spread = upfront = 0
    and `has_quotes` is never 'Y'.
  - Transform table: `ACI` has only empty 6M `EOD-Interp` stub rows (all price columns NULL) from 2026-03-18, no 5Y row.
    (Not verified why; likely the transform treats Markit's zero as no-quote.)
  - So HY47 shows `ACI` as all-NaN rows (`has_data = False`) on every date. The Markit website file does show `ALBERCO`, so
    fill from there via `config/overrides.csv` if needed. `NEWALB`/`NEALBE` (New Albertsons LP) is a different entity with full history.
  - Bloomberg (`bdh_mm('ACI B CDS USD SR 5Y D14 Corp', 'PX_LAST')`, curve CZ066173) also starts only 2026-09-25: 21 days,
    PX_LAST 180 on 2026-10-23 (bid 170 / ask 190). So the curve is new (late Sep 2026); there is no earlier history anywhere.
    HY47 launched 2026-09-28, so BBG covers its full life.
  - Other always-missing HY47 names (BLDR, DTV, NXST, EA, SOLEIN, TLN) not yet checked for the same pattern.

## Price source waterfall (agreed 2026-10-25; design only, not yet implemented in code)

Goal: every index member has a price on every date. Priority order, highest first. For each (name, date) the highest source that has a
value wins; a lower source is used ONLY where every source above it has nothing (today "nothing" = `has_data = False` in
`cache/spreads.parquet`). Planned: a `price_source` column on each row
(`manual` / `databricks` / `markit_addin` / `bloomberg`) so the origin of every number is visible.

1. **Manual override** `config/overrides.csv`, hand-entered, keyed on (ticker, date, maturity). Always wins, so it can also correct Databricks
   values that are present but wrong ("garbage"), not just fill gaps. Overrides never get overwritten by refreshes (separate file).
2. **Databricks** `cds_transform_interpolated` (what `update_store()` pulls today). Has the interpolated half-year tenors (`EOD-Interp`).
3. **Markit Excel add-in** (same vendor as Databricks' source, but queried directly; may bypass the pipeline's zeros/stubs).
   - Guide + data dictionary in `docs/markit_excel_guide/`. Add-in is registered in Excel (`_IHSMarkitComAddin.AddinModule`); whether it is
     loaded and logged in is untested. Python has `win32com` and `xlwings` for driving Excel.
   - Status 2026-10-25: WORKING after the user logs in (IHS Markit ribbon > Login; login persists across Excel instances). Installed v6.6.2.2 in
     `%LOCALAPPDATA%/Programs/IHS Markit/IHS Markit Add-In for Microsoft Excel/` but Excel's load setting is `LoadBehavior=2` (not loaded), so a fresh
     xlwings instance needs `app.api.RegisterXLL(<path to ihsmExcel6x64.xll>)` first (session only), else `IHSM` is `#NAME?`. Never script credentials.
   - Verified vs Databricks on ADT (`THADT/SNRFOR/USD/XR14/5Y/500`, 2026-10-23/10-01/09-28): conv_spread, upfront, par_spread, risky_pv01 and
     default_probability all match. `IHSMHistory(id, field, "cdspd", , DATE(y,m,d))` returns one value per date (ADT back to at least 2026-03-02).
   - Units from the add-in (default Preference): spreads are decimals (0.013132 = 131.32 bp, multiply by 10000), upfront is a fraction (-0.16242 =
     -16.24 points, multiply by 100), `runningCoupon` 0.05 = 500 bp, `asOf` is an Excel date serial, `assumedRecovery` 0.4, `riskyPv01` as-is.
   - ALBERCO (ACI): the add-in HAS real prices where the silver table has only zeros: 2026-10-23 conv_spread 194.1 bp, upfront -13.1, risky_pv01 4.40,
     default_prob 0.172; history starts 2026-09-24/25 (`#EMPTY!` on 2026-09-23). Bloomberg PX_LAST differs from Markit by about 10-20 bp on the same
     dates (BBG 180 vs Markit 194 on 2026-10-23; 205 vs 186 on 2026-10-01), so don't mix sources inside one name's history without noting it.
   - Tenors: the identifier takes a standard tenor node, NOT a maturity date. Tested 2026-10-25 on THADT: `2Y`, `3Y`, `5Y` return data; `30M`, `2Y6M`, `2.5Y`,
     `2Y 6M` all return a blank (rejected, no error code). So an older index (e.g. HY42, maturity 2029-06-20, 2Y6M on 2026-10-23) cannot be queried directly.
     The tenor to use also moves with the date (maturity minus date), and the 5Y node rolls on each IMM date, so the tenor must be derived per date from the
     index maturity. Only HY47 (5Y since its 2026-09-21 roll) is a plain 5Y query. Verified on ADT 2026-10-23: Databricks' 2Y6M row (EOD-Interp, 2029-06-20) has
     conv_spread, par_spread and risky_pv01 equal to the simple average of its 2Y and 3Y rows (67.4619 = (56.6716 + 78.2521)/2); upfront is NOT a plain average
     (-10.7568 vs -10.711), so it is recomputed. Plan for half-year points: query the bracketing annual nodes and average, using the same weights as Databricks.
   - Full store built 2026-10-25 (`update_store()`, all 40 indices): 2,549,875 rows, 187,107 (7.3%) with no Databricks data; 117 index-name pairs
     (35 tickers, 89,637 cells) never have data, 561 pairs (97,470 cells) have gaps. 4,876 real rows (11 names) have conv_spread = 0 (wide names).
   - Add-in test on the 35 never-priced tickers (5Y node, primary + alt coupon; latest date + 2022-06-01, 2023-06-01, 2024-06-03, 2025-06-02):
     34/34 returned a latest-date price (COX skipped: no `Markit_5Y_ID` in the mappings file). History at the 5Y node was present for 12/34 on 2022-06-01,
     16/34 on 2023-06-01, 20/34 on 2024-06-03, 18/34 on 2025-06-02; the rest `#EMPTY!`. CAVEAT: those dates were not restricted to each name's index
     lifetime, so the `#EMPTY!` counts overstate real gaps (e.g. NVDA, SPCX, ACI only joined recent indices). Names with full 2022+ add-in history that
     Databricks lacks: APACHE, AR, EQM, FSA, GKNLN, HWM, MAR, MGIC, NVFVES, UBER (+ BLDR, TMO partly). TLN returned a blank (not `#EMPTY!`) for every history
     date: unresolved, retest individually. Not yet tested at each index's actual tenor/maturity window.
   - DECIDED 2026-10-25: for half-year tenors missing from the add-in, linearly interpolate `conv_spread` and `risky_pv01` between the bracketing annual
     nodes (same weights as Databricks' EOD-Interp). Upfront is not interpolated (not a plain average); leave it blank or derive it later.
   - Add-in vs Databricks on ADT 2026-10-23 is exact at 5Y (131.32 bp) but off by 0.1-0.2 bp at 2Y (56.48 vs 56.67) and 3Y (78.33 vs 78.25): likely a different
     batch than Databricks' EOD. Pin the batch before relying on it.
   - Coverage test, HY47 names with no Databricks data (2026-10-25, 5Y at the index coupon 500 AND the other coupon 100): the add-in returned a full
     latest-date record (conv_spread, upfront, par_spread, risky_pv01, default_prob) for 7/7 (ACI, BLDR, DTV, EA, NXST, SOLEIN, TLN), for BOTH coupons,
     so a name Databricks only holds at one coupon can still be priced at the index coupon. Dated history is patchier: on 2026-10-01 all 7 returned
     a value; on 2026-09-28 (HY47 launch) ACI, BLDR, DTV did, EA/NXST/SOLEIN gave `#EMPTY!` and TLN a blank. Full-window gaps not yet measured (use IHSMTimeseries).
   - Wide names (CSCHLD, HTZ, XRX): the add-in ALSO returns a blank conventionalSpread (upfront 71-79 pts, par/rpv01/pd present and close to Databricks),
     so it does not remove the need for the recovery-adjusted re-solve. XRX at coupon 500 returned odd conv values on 10-01/09-28 (25,746 / 12,790 bp): distrust.
   - Calling `IHSM` without a batch gives the 'latest' batch, which can differ slightly from Databricks EOD for wide names (CSCHLD upfront 78.96 vs 78.92);
     ADT matched exactly. Pin the batch before using the add-in as a like-for-like source.
   - Functions: `IHSM` (latest), `IHSMHistory` (one value, by date or offset), `IHSMTimeseries(ids, fields, "cdspd", batch, start, end, ascending, limit)`.
   - CDS single-name identifier = `Ticker\Tier\Ccy\DocClause\Tenor\Coupon`, same format as the `Markit_*_ID` columns in `cds_mappings_final.csv`
     (e.g. `RAKUGRO\SNRFOR\JPY\CR14\5Y\100`). Namespace `cdspd`. Field paths MUST be prefixed `curve.`, e.g. `curve.curvePoints.conventionalSpread`; the unprefixed form gives `#RES!`.
     Verified fields: `curve.curvePoints.{conventionalSpread, upfront, parSpread, riskyPv01, defaultProbability, tenor}` and top-level `asOf`, `assumedRecovery`,
     `runningCoupon`, `docClause`.
   - Timeseries limits: 50 instruments, 50 fields, 20 years, 500,000 (instruments x fields x days), 100,000 rows, default `limit` 10,000.
   - Keyed by standard tenor, not maturity date, so half-year tenors (1Y6M, 2Y6M, ...) for older indices will need interpolating.
   - The add-in's Preference setting controls spread format (bp) and upfront format (%); check it matches the Databricks units (spreads in bp).
4. **Bloomberg** `bdh_mm('<BBG_5Y_CDS_Ticker>', 'PX_LAST')` (`LAST_PRICE` is identical). `PX_LAST` is the conventional spread in bp.
   - Mappings file column `BBG_5Y_CDS_Ticker`, e.g. `ACI B CDS USD SR 5Y D14 Corp`. It is a 5Y ticker only, so older indices
     (whose maturity is now e.g. 2.5Y) probably have no matching tenor; this is the known weakness of this source.
   - Gives a spread only (plus bid/ask); no upfront, recovery or risky_pv01.

Fields a filled row can lack depending on source: Bloomberg gives only the spread, so `upfront`, `all_upfront`, `risky_pv01`,
`default_probability` and recovery would have to be derived from it with the Quantifi Toolkit (planned; see `risky_pv01` notes below).

### Notes on risky_pv01 / wide names (2026-10-25)
- `all_upfront` = `upfront` + `running_coupon`/100 x `risky_pv01` (verified exact on CSC, Hertz, Xerox); `risky_pv01` is the risky duration
  (coupon stream discounted and survival-weighted), at most about the risk-free 5Y annuity (~4.5) and much lower for names likely to default.
- Wide names (HY47: CSC Holdings, Hertz, Xerox) have `conv_spread = 0` because `all_upfront` (73-83) exceeds the 40%-recovery cap of
  100 x (1 - 0.40) = 60 points, so no spread solves. Plan: re-solve at a lower recovery (100 - `all_upfront` - 5 points) with the Toolkit,
  recomputing `risky_pv01` in the same step because it depends on recovery. Treat `conv_spread == 0` as missing until then.

## Notebooks
- `index_spread_moves.ipynb` (the one notebook; the older `single_name_spread_moves.ipynb` was deleted 2026-10-25): pick an index + two dates; first shows which names
  are missing data (red box + table: never priced / `conv=0` / gap), then per member the `conv_spread`, `upfront`, `all_upfront` and `risky_pv01` at both dates and
  each change, sortable by any of the four (`SORT_BY`), flagged rows shaded red. Reads `cache/spreads.parquet` via `cdx_spreads.spread_changes` (treats conv_spread 0
  as missing, applies `config/overrides.csv`). Tables come from `cdx_spreads.show_table` (plain HTML, no jinja2: the project `.venv` doesn't have it).

### Wide-name recalibration (built 2026-10-25): `cds_recalib.py`
- For rows where Markit publishes `conv_spread = 0` (11 names, 4,876 rows in the store: ALTICE, BHCCN, CSCHLD, CVNA, CYH, HTZ, IHRT, LUMN, RIG, VMED, XRX) we
  fit a one-node survival curve with the Quantifi Toolkit (`SurvivalCurve.FitCDSQuotes`, `CDSQuoteType.Upfront`, SNAC/STEC terms) to the row's `upfront` at its
  coupon and maturity, with **recovery (%) = 100 - all_upfront - 5** (floor 0%, never hit so far; range 6-33%, median 21.5%) and a **flat 4% discount curve**
  (user's assumption; no ISDA curve source found). Replacement `conv_spread` = `CurveUtil.ImpliedSpread` at the maturity, `risky_pv01` = `CurveUtil.ImpliedDuration`.
- The recovery mark is wrong by design (it just makes the fit solvable); the goal is a clean comparable mark. Because recovery floats with `all_upfront`, a name's
  recalibrated spread change includes the effect of its recovery moving between the two dates (e.g. Xerox 26.2% -> 21.2% on HY47).
- The Toolkit is installed in the SYSTEM Python (module `Quantifi`), not the project `.venv`. So run `python pricing/shared/cds_recalib.py` with the system Python (after
  `update_store()`); it writes `cache/recalibrated.parquet` (gitignored). `cdx_spreads.spread_changes` reads that file with no Toolkit needed and marks those rows
  state `recal` (amber in the notebook, with the recovery used); without the file they stay `conv=0` (red). A manual override beats both. Re-run it whenever the store gains new rows.
- Validation: same method at forced 40% recovery on the 90 normal HY47 names (2026-10-23) fitted 90/90 and reproduced Markit conv_spread with median error +1.0 bp
  (0.29%), 90th percentile 5.1 bp, worst 8.7%; risky_pv01 median error -0.065. On ADT the effective discount rate Markit used is ~3.7-4.4%, so flat 4% is reasonable.
- Not covered: names with no Databricks row at all (see the waterfall; the Markit add-in is the next source), and the add-in's own blank conv_spread for wide names.

### Pricer: `cds_pricer.py` (built 2026-10-25)
- `CdsCurvePricer(asof, maturity, ccy, par_spread=, risky_pv01=, upfront=, conv_spread=, coupon=)` resolves a canonical (par_spread, risky_pv01) from whatever is given
  (order: par+rpv01, upfront+rpv01, conv+rpv01, then par / upfront / conv alone with risky_pv01 derived from a flat Quantifi curve; user OK'd deriving risky_pv01),
  then `.at_coupon(C)` returns par_spread, risky_pv01, upfront, all_upfront, conv_spread, conv_status, recovery_used, route at ANY coupon, and `.curve()` returns a
  calibrated Quantifi `SurvivalCurve` (flat across tenors, one node at the maturity) to plug into other Toolkit functions. `price_rows(df, target_coupon)` does a DataFrame
  (~2.5 ms/point); `price_store()` re-prices every priced store point at its index coupon into `priced_at_index_coupon.parquet` (not yet run on the full store:
  ~1M unique points, roughly an hour). Run it with the SYSTEM Python (Toolkit is not in the .venv).
- Upfront(C) = (par - C) x rpv01 / 100 and all_upfront = par x rpv01 / 100 are used for the coupon switch; conv_spread(C) is a Toolkit flat-curve conversion of upfront(C).
  conv_spread is converted at the standard 40% recovery; the Toolkit raises SurvivalFitException exactly for the wide names Markit gives conv_spread = 0 (CSC, Hertz, Xerox),
  and only then is the marked-down recovery rule used (flag 'recal'). Do NOT pre-check feasibility: iHeart has all_upfront 65 > 60 and still solves.
- Validation (2026-10-23): re-pricing the 90 normal HY47 names at their own coupon from par+rpv01 gave all_upfront error 0.000, upfront max 0.044 pts, conv median +1.0 bp
  (mean abs 5.8, p90 5.2). Calibrate at the 500 record and predict the add-in's 100 record (7 names): par+rpv01 -> upfront 0.01 pts / conv 2.5 bp max; upfront+rpv01 0.03 / 2.7;
  conv+rpv01 0.16 / 4.7; conv only (rpv01 derived) 0.39 / 11 bp (conv runs ~9 bp low because conv_spread depends on the coupon). Prefer par+rpv01 when we have it.
- risky_pv01 is stored to 2 decimals; refining it as all_upfront x 100 / par helps wide names but hurts tight ones (upfront off by up to 0.29 pts), so it is used as stored.

### Integration (2026-10-25)
- `python pricing/shared/cds_pricer.py` (system Python) writes `priced_at_index_coupon.parquet` (all priced points re-priced at their index coupon, resumable per index via `cache/_priced_parts/`).
  `cdx_spreads.spread_changes` then shows every spread / upfront / all_upfront / risky_pv01 AT THE INDEX COUPON with states ok / recoupon (quoted at the other coupon, blue) /
  recal (wide name, marked-down recovery, amber) / missing, incomplete (red). Without the file it falls back to the Markit coupon (and `cds_recalib.py` / `conv=0`).
  On HY47 (2026-09-29 -> 2026-10-23): 68 priced at 500, 22 re-priced from 100, 3 wide names, 7 never priced. `cds_recalib.py` is now superseded by the pricer for the display.

### Add-in coverage test on the unpriced cells (2026-10-25) and `markit_addin.py`
- `markit_addin.py` (system Python; drives a separate Excel window via xlwings): `tenor_map()` (Databricks tenor label per (date, maturity), cached), `node_plan(label)`
  (half-year label -> 50/50 of the two annual nodes), `build_id(ticker, node, coupon)` (from the mappings file's Markit_5Y_ID), `AddinSession.pull()` (bulk `IHSMTimeseries`, up to 40
  ids per call, bisects a failing batch to isolate bad ids), `pull_batches()` (cached per batch in `cache/addin_raw/`), `load_raw()` (tidy: conv/par in bp, upfront in points, rpv01).
- GOTCHA: never call Calculate() while waiting on the add-in; it restarts the async fetch and the array never lands. Wait for the `IHSM [RxC]` marker, then for the array. The login persists across
  Excel windows (the `!E##LOGIN` text is normal in the formula cell). A formula cell with a multi-id range returns one stacked table: Identifier, asOf (Excel serial), fields.
- Sample: 55 (index, name) pairs (20 never-priced, 25 partial-gap, 10 priced for validation) -> 24,934 cells, 150 instruments, 0 failed ids, 1.2 min of Excel time.
- Fill rate on the 24,369 unpriced sample cells, queried at the INDEX coupon: **77.5% priced** (never-priced pairs 81.8%, gap pairs 65.7%; HY 85.5, XO 83.9, IG 72.6, MA 37.6%; 2022 55%
  rising to 91.5% in 2026; annual 76.9% vs half-year 78.3%; only 19 cells had one of two nodes missing). Extrapolated (sample is pairs, so a rough guess): ~137k of the 187k unpriced cells,
  taking coverage from 92.7% to about 98%.
- Validation on 445 priced cells (add-in vs Databricks, same tenor logic): conv_spread median error 0.00 bp, mean abs 0.19, p90 0.38 (annual and interpolated half-year alike); par_spread mean abs
  0.20 bp; risky_pv01 identical. So the tenor mapping and 50/50 interpolation are right.
- Add-in `riskyPv01` only exists from 2025-09-01; before that conv, par and upfront are 100% present but rpv01 is blank, so older cells need risky_pv01 derived (cds_pricer route:
  par / conv / upfront + coupon; the identity upfront = (par - C) x rpv01 / 100 is ill-conditioned when par is near the coupon, i.e. IG at 100).
- Unfillable by the add-in: COX (no Markit_5Y_ID in mappings), BRITEL (MA37, 1,128 cells; BRITIST EUR MM14), MDC (IG40, 694; mapped to SEHOUS, Sekisui House US, likely a renamed entity), AAL, BRSL, ROSINI,
  LDOIM, MET and MSI (single cells on 2022-04-11, a date Markit seems to be missing). ILDFP, UNITI, STAR, COFB, STGATE are only partly covered (curve starts later than the index). 5,461 of the 5,480
  unfilled cells had no add-in row at any node: identifier/entity problems, not interpolation.

### Full add-in fill (2026-10-25): `addin_fill.py`, `config/ticker_aliases.csv`, and ticker changes
- `python pricing/fix_history/addin_fill.py plan|pull|assemble` (system Python). Pull is incremental (skips ids already in `cache/addin_raw/`), batches are named by content. 767 instruments, ~1.26M rows, 6 min of Excel.
  Assemble interpolates half-year tenors (50/50 on conv/par/rpv01), derives risky_pv01 (add-in has it only from 2025-09-01) and upfront with `cds_pricer`, writes `cache/addin_fill.parquet`
  (key: index_name, market_cds_ticker, published_date, cds_maturity; columns conv_spread, par_spread, risky_pv01, upfront, all_upfront, route, rpv01_source, source='markit_addin') and
  `cache/addin_unfilled.parquet`. Result: **133,549 of 187,392 unpriced cells filled (71.3%)**: HY 71.3, IG 72.9, XO 73.3, MA 62.2%. 34,358 have the add-in's own risky_pv01, 99,190 derived.
  With it, priced coverage goes 92.7% -> 97.9% (2,496,032 of 2,549,875 cells). NOT yet joined into the notebook / spread_changes (waterfall: Databricks first, then this file, then overrides on top).
- Ticker changes: `markit_addin.build_id(ticker, node, coupon, date)` reads the USER's `S:/.../cds_mappings/markit_ticker_delta_final.csv` (change_date, market_cds_ticker, new_markit_ticker,
  old_markit_ticker: old before change_date, new from it) plus `config/ticker_aliases.csv` (project-local extras; a ticker listed there uses only those rows; currently COX: COX-CommInc to 2026-09-14,
  COXCOM from 2026-09-15, Cox changed after the delta file's last edit). Databricks' transform already stitches most delta-file names (ABX, SPG, RIG ... have no gaps) but omits Cox entirely.
- Remaining 53,843 unfilled cells (123 tickers) are almost all PRE-successor dates: the current Markit ticker starts the day after the old curve ends (TIBX CLOUSOAA 2026-09-15, CPN CALPLLC
  2026-02-17, ET ENERTRAK 2026-08-27 ...). `cache/proposed_ticker_changes.csv` (same format as the delta file; 11 rows, name match 0.8-1.0, ~33k cells) lists predecessors found in Markit's raw
  table: ET->ENERTRAE, BRITEL->BRITEL-BritTel, CPN->CPN, UNITI->UNITIGR, ILDFP->ILIAHOL, TIBX->CLOUDSO, HIG->HIG, CIVI->CIVIRES, plus BRSL (=IGT row), NJJ_HLD (=MATTER row), 'MLTIVR A' (=MLTIVR).
  Not yet merged anywhere: the user decides whether they go into the delta file. No predecessor found for SIRI, SUNN, ADLERR, PPFTEL, EOFP, RTX, ATLIM, SRE, ASH, EDPPL, LTMCIM, BMCAUS B, FIBCOP, HOUS,
  GSK, MLFP, ELOFR (need manual research, or these have no earlier curve at all).
- Full pricing run (`cds_pricer.price_store`, 44 min): 2,362,483 points at the index coupon; 2,357,900 conv ok, 4,515 wide names at a marked-down recovery, 68 failed SurvivalFitException even then.

### Update 2026-10-25 (after merging the 11 proposed ticker changes into the user's delta file)
- The 11 rows from `cache/proposed_ticker_changes.csv` were appended to `S:/.../cds_mappings/markit_ticker_delta_final.csv` (now 21 rows; backup in `cds_mappings/old/markit_ticker_delta_final_backup_2026-10-25.csv`;
  notes start 'added 2026-10-25'). Cox is still only in the project-local `config/ticker_aliases.csv`. The user will double-check the matches (HIG and CIVI are 0.83, the rest 1.00 or same-entity rows).
- Add-in fill is now **166,956 of 187,392 unpriced cells (89.1%)**: HY 90.2, XO 90.5, MA 87.6, IG 86.6%; 42,474 with the add-in's own risky_pv01. The 11 re-aliased names are essentially fully filled
  (ET 5,711, BRITEL 5,522, CPN 4,466, UNITI 3,905 new, ILDFP 3,757, BRSL 3,499, TIBX 2,685, NJJ_HLD 1,821, MLTIVR A 952, HIG 755, CIVI 334; COX 6,089). Previous fill kept at `cache/addin_fill_before_aliases.parquet`.
- Priced coverage with Databricks + this fill: 2,529,439 of 2,549,875 cells = **99.2%**. Still unpriced: **20,436 cells in 122 tickers**; top MDC 3,966 (successor SEHOUS; predecessor M.D.C. Holdings not found
  yet, the name-match picked UNITIGR wrongly), SIRI 2,213, EXE 1,421, SUNN 1,264, ADLERR 1,220, PPFTEL 1,160, AEGON 980, COMM 956, DSFIR 752, EOFP 737, COFB 701, RTX 680, ATLIM 635, SRE 617, HEIGR 560.
  These had no obvious predecessor curve (see the list above) and need manual research or are genuinely unpriced. Remaining plan: join the fill into the notebook (waterfall), research the leftovers,
  fix the 68 pricing failures, then bulk approximate overrides for whatever is left.

## Index-level metrics (one of the main purposes of this workbook) — `index_metrics.py`
Built from each single name's **all_upfront**, which is its present value of expected loss ("expected_loss"), in points. It is coupon-free (all_upfront = par_spread x risky_pv01 / 100), so no
coupon choice is involved. For each (index, date) there is exactly ONE row (`cache/index_metrics.parquet`, `index_metrics.all_index_metrics()`, 24,415 rows over the 40 indices):
- **dispersion** = standard deviation of all_upfront across the index's priced names (unweighted; POPULATION std, ddof=0, divide by N, chosen by the user 2026-10-25; `index_metrics.DDOF = 1` for the sample version, about 0.4-0.7% larger).
- **index expected loss** (`index_el`) = sum over the priced names of weight x all_upfront (weights are fractions of the index: 1/125 IG and MA, 1/100 HY, 1/75 XO).
- **topN_share** (`top5_share`, `top10_share`) = (sum of weight x all_upfront over the N names with the largest all_upfront) / index_el. Example: if XRX is 69 pts, CSCHLD 80, HTZ 75, UFS 55 and IHRT 60,
  the top 5 are those five and top5_share = their weighted expected loss / the index's total weighted expected loss.
- A name with no all_upfront on a date is EXCLUDED from that date (`n_priced`, `priced_weight` say how much of the index the numbers cover), so index_el and the shares cover the priced part only.
  all_upfront comes from Databricks (Markit's stored value) where priced there, else from the Markit add-in fill (`addin_fill.py`).
- Defaulted names are INCLUDED (built 2026-10-25): Bloomberg's roster drops them (weight 0), so `index_metrics` adds each name from `teams.structured_credit_abhutra.credit_events` back at the original weight (1/N) for every date before its `index_ex_date`, with its own prices. `n_members` is the number of LIVE names on the date (HY38 starts at 100, falls as names default), `index_el` divides by the live weight, `priced_weight` is the priced share of it.
- Notebook section 4 shows the metrics at the two dates plus the 10 widest names and their share of the index EL (`index_metrics.widest`). Example HY47 2026-10-23: dispersion about 16.00 (population), index EL 15.46 pts,
  top5 23.1%, top10 36.1%.

### Recovery rule changed to continuous (2026-10-25)
- `cds_pricer` now converts every upfront to a conv_spread at recovery = min(40%, 100 - all_upfront - 5) (`CONTINUOUS_MARKDOWN = True`), not 'try 40%, mark down if the fit fails'. Near the edge of what
  40% allows the standard conv_spread explodes (LUMN at all_upfront 71 solved to 19,000 bp, XRX up to 35,000) and then stops solving, so a move across that edge mixed two bases (Xerox on HY47 showed -9,282 bp).
  With the continuous rule it is +706 bp. Normal names (all_upfront < 55) are unchanged (40%). 15,025 points with all_upfront > 50 were repriced in place (`python pricing/shared/cds_pricer.py rewide`; 11,069 of them are now
  'recal'); backups: `cache/priced_at_index_coupon_before_continuous.parquet`, `cache/addin_fill_before_continuous.parquet`. Where Markit publishes a conv_spread for a name with all_upfront 55-65 (e.g. IHRT
  7,011 bp) our number now differs from Markit's because of the marked-down recovery; that was the user's stated trade-off (clean and comparable over matching Markit).

## Credit events table: `teams.structured_credit_abhutra.credit_events` (noted 2026-10-25)
- Managed Delta table, owner mmoore@hudsonbaycapital.com, 74 rows (checked 2026-10-25): one row per credit event per index series the name was in.
- Columns: `sn_ticker` (the single name's market_cds_ticker, e.g. TLN, SFRFP), `index_name` (e.g. HY38, XO35; includes older series than the 40 in config/cds_index.csv), `auction_date`,
  `index_ex_date` (the day after the auction: the name leaves the index), `index_loss` and `index_recovery` (both in index-notional units, e.g. TLN in HY38: loss 0.0040, recovery 0.0093).
- Example: TLN (Talen Energy Supply) in HY38: auction 2022-06-07, ex-date 2022-06-08. That is why TLN has no data from then and why defaulted names have weight 0 in the Bloomberg rosters.
- USE (not built yet): it is the missing piece for the roster caveat in the index-metrics section. A name that defaulted should be kept in its series at its ORIGINAL weight (1/N) up to
  `index_ex_date` and dropped from then on, instead of being left out of the whole history. That fixes dispersion and the top-N expected-loss share for dates before a default, and
  `index_loss` / `index_recovery` give the realised loss to add to index-level metrics. Join key: (sn_ticker = market_cds_ticker, index_name).

### Defaulted-names fix: files and commands (2026-10-25)
- `pricing/fix_history/defaulted.py` (`refresh_defaulted()`; venv, needs a Databricks login) caches `cache/credit_events.parquet` (56 events in our 40 series) and `cache/defaulted_prices.parquet` (Databricks all_upfront at the series maturity from launch to
  index_ex_date - 1; only 12,625 of 25,598 expected name-days: SFRFP 90%, DISH 97%, ARGID 100%, nothing for ATOS, AVP, CASINO, DSPORT, LINTA, INTRUM, NFE, NFEFIN, RAD, TLN, since Markit's raw table has thin history for them).
- The rest comes from the Markit add-in: `python pricing/fix_history/addin_fill.py pull-defaulted` then `assemble-defaulted` (system Python) -> `cache/addin_fill_defaulted.parquet` (11,669 of 12,973 missing name-days, 89.9%). Those names are not in
  `cds_mappings_final.csv`, so their Markit tickers/tier/ccy/doc are in the project-local `config/ticker_ids.csv` (ATOS, AVP, CASINO->GROUPE, DSPORT->DIAMSPO, INTRUM->INTRUAB, LINTA->LITLC, NFEFIN, NFE->NEWFORAB, RAD; TLN is in the mappings).
  The add-in has full pre-default history for all of them (par_ok rows match each name's life up to its auction date).
- Wide-name rule in `addin_fill.assemble` (applies to the main fill too): a par-based all_upfront saturates near 60 (a flat 40%-recovery model cannot exceed it; par above ~1,100 bp), so where the par-based value
  exceeds 50, or the flat-curve fit fails (par above ~25,000 bp, just before default), all_upfront = the add-in's own upfront + coupon x risky_pv01 (route 'addin upfront (wide / fit failed)'). Gave 71-100 points just before default
  (DSPORT median 91, RAD 78, CASINO 67, NFE 75) instead of a flat ~60.
- Effect: HY38 on 2022-05-31 has 100 members (was 94), dispersion 8.68 -> 12.14; XO40 July 2024 dispersion 7.05 -> 14.02 (Atos still in the index); XO37 2025-01-15 4.87 -> 8.68. 9,439 of 24,415 index-dates changed.
  Coverage: median priced_weight 1.00, 5th percentile 0.96; 693 index-dates are below 0.95, mostly XO37 in 2022-23 (min 0.76) and the 2022-04-01..11 Markit data hole. 1,304 defaulted-name days are still unpriced (SFRFP 342,
  INTRUM 211, DISH 184, NFEFIN 161, LINTA 125, AVP 89, ATOS 52, RAD 46, DSPORT 36, CASINO 31, NFE 26).
- Main add-in fill rebuilt with the wide-name rule: 1,123 cells changed all_upfront (ALTICE 802, COMM 276, NVFVES 33, MERLLN B 7, IHRT 3, LUMN 2; e.g. ALTICE median 53.8 -> 60.8); index metrics recomputed afterwards
  (dispersion moved on 1,123 index-dates, max 0.81; HY47 2026-10-23 unchanged: dispersion 16.00, index EL 15.46, top5 23.1%, top10 36.1%). Backup: `cache/addin_fill_before_wide_rule.parquet`.
- Notebook tables (`cdx_spreads.show_table`): plain rows and the header are white text on black; tinted rows and the boxes above the table are black text on a light tint (2026-10-26), so every cell has an explicit
  readable color in any notebook theme (black = Databricks at the index coupon, blue = re-priced from the other coupon, purple = Markit add-in, amber = wide name at a marked-down recovery, red = no price). Cells and headers are centered (`show_table(..., align='auto')` for right-aligned numbers and left-aligned text).
