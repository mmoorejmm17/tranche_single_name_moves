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
- TODO (confirm with user): spread units (bp vs decimal), which of `par_spread`/`conv_spread` is the
  standard to use, and whether to prefer `*_derived` columns over the raw ones for interpolated rows.
