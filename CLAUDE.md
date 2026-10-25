# tranche_single_name_moves

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
    fill from there via `overrides.csv` if needed. `NEWALB`/`NEALBE` (New Albertsons LP) is a different entity with full history.
  - Bloomberg (`bdh_mm('ACI B CDS USD SR 5Y D14 Corp', 'PX_LAST')`, curve CZ066173) also starts only 2026-09-25: 21 days,
    PX_LAST 180 on 2026-10-23 (bid 170 / ask 190). So the curve is new (late Sep 2026); there is no earlier history anywhere.
    HY47 launched 2026-09-28, so BBG covers its full life.
  - Other always-missing HY47 names (BLDR, DTV, NXST, EA, SOLEIN, TLN) not yet checked for the same pattern.

## Price source waterfall (agreed 2026-10-25; design only, not yet implemented in code)

Goal: every index member has a price on every date. Priority order, highest first. For each (name, date) the highest source that has a
value wins; a lower source is used ONLY where every source above it has nothing (today "nothing" = `has_data = False` in
`cache/spreads.parquet`). Planned: a `price_source` column on each row
(`manual` / `databricks` / `markit_addin` / `bloomberg`) so the origin of every number is visible.

1. **Manual override** `overrides.csv`, hand-entered, keyed on (ticker, date, maturity). Always wins, so it can also correct Databricks
   values that are present but wrong ("garbage"), not just fill gaps. Overrides never get overwritten by refreshes (separate file).
2. **Databricks** `cds_transform_interpolated` (what `update_store()` pulls today). Has the interpolated half-year tenors (`EOD-Interp`).
3. **Markit Excel add-in** (same vendor as Databricks' source, but queried directly; may bypass the pipeline's zeros/stubs).
   - Guide + data dictionary in `markit_excel_guide/`. Add-in is registered in Excel (`_IHSMarkitComAddin.AddinModule`); whether it is
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
- `index_spread_moves.ipynb`: pick an index + two dates, shows start/end `conv_spread`, change (bp and %) per member, widest first, with a `status` column
  for names that are `missing` or `conv=0`. Reads `cache/spreads.parquet` via `cdx_spreads.spread_changes` (treats conv_spread 0 as missing, applies `overrides.csv`).
  `single_name_spread_moves.ipynb` is the older version of the same idea (kept, untracked).
