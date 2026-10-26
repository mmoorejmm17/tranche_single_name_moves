"""WRITE side of the store: pull the index rosters / maturities (Bloomberg) and the single-name rows (Databricks) into the working copy cache/.

Only the owner runs this. The analysis code never imports it; it reads the snapshot that publish.py copies from cache/ into data/.
Run from the project root with the venv or system Python: python -c "import sys; sys.path.insert(0, 'pricing'); import cdx_store; cdx_store.update_store()"
"""
import _paths  # noqa: F401  (must come first: points cdx_spreads at the working copy cache/ and config/)
import sys

import pandas as pd

from cdx_spreads import DATA_DIR as CACHE_DIR, SPREAD_COLS, STORE_COLS, STORE_FILE, index_coupon, load_index_list

TABLE = 'teams.structured_credit_abhutra.cds_transform_interpolated'
REPULL_DAYS = 7                                   # re-fetch this far back on update in case Databricks restated
HISTORY_START = '2022-04-01'  # table starts here

# own utils: not pip-installed, so add the repo roots to sys.path (imported lazily: only needed to write)
_UTIL_PATHS = ('S:/Structured Credit/Matt Stuff/claude_repos/bbg_utils',
               'S:/Structured Credit/Matt Stuff/claude_repos/databricks_utils')


def _utils_on_path():
    for _p in _UTIL_PATHS:
        if _p not in sys.path:
            sys.path.insert(0, _p)


def bdp_mm(*args, **kw):
    _utils_on_path()
    from bbg_utils import bdp_mm as f
    return f(*args, **kw)


def get_conn(*args, **kw):
    _utils_on_path()
    from databricks_utils._connection import get_conn as f
    return f(*args, **kw)


def get_index_members(*args, **kw):
    from cdx_members import get_index_members as f
    return f(*args, **kw)


def _bbg_ticker(index_name):
    idx = load_index_list().set_index('index_name')['bbg_ticker']
    if index_name not in idx.index:
        raise KeyError(f"{index_name!r} not in cds_index.csv; options: {list(idx.index)}")
    return idx[index_name]


def get_index_maturity(index_name, force_refresh=False):
    """The index's 5Y maturity date (BDP MATURITY). Static per series, so cached indefinitely."""
    CACHE_DIR.mkdir(exist_ok=True)
    path = CACHE_DIR / 'index_maturity.csv'
    cache = pd.read_csv(path, index_col='index_name')['maturity'] if path.exists() else pd.Series(dtype=str, name='maturity')
    if force_refresh or index_name not in cache.index:
        mat = bdp_mm(_bbg_ticker(index_name), 'MATURITY')['MATURITY'].iloc[0]
        cache.loc[index_name] = pd.Timestamp(mat).strftime('%Y-%m-%d')
        cache.rename_axis('index_name').to_csv(path)
    return pd.Timestamp(cache[index_name])


def get_constituents(index_name, force_refresh=False):
    """Non-zero-weight members (zero weight = defaulted, no longer in the index). A series' roster is static, so cached."""
    CACHE_DIR.mkdir(exist_ok=True)
    path = CACHE_DIR / f'members_{index_name}.csv'
    if not force_refresh and path.exists():
        m = pd.read_csv(path, dtype=str)
        if 'markit_cds_ticker' in m.columns:   # rosters cached before this column existed get refetched
            return m.assign(weight=lambda d: pd.to_numeric(d['weight']))
    m = get_index_members(_bbg_ticker(index_name))
    m['weight'] = pd.to_numeric(m['coupon_or_weight'])
    m = m.loc[m['weight'] > 0, ['index_ticker', 'member_name', 'weight', 'currency', 'seniority',
                                'bbg_cds_ticker', 'sp_id', 'Market_CDS_Ticker', 'markit_cds_ticker']].reset_index(drop=True)
    m.to_csv(path, index=False)
    return m


# (anchor series, anchor launch date): launches are every 6 months, on the 20th (IG/XO/MA) or 27th (HY) of Mar/Sep
LAUNCH_ANCHORS = {'IG': (47, '2026-09-20'), 'HY': (47, '2026-09-27'), 'XO': (46, '2026-09-20'), 'MA': (46, '2026-09-20')}


def index_launch_date(index_name):
    """Nominal launch date: anchor date less 6 months per series, weekends rolled to the next Monday.

    Holidays aren't handled here; _grid snaps to the first published_date on or after it.
    """
    family, series = index_name[:2], int(index_name[2:])
    anchor_series, anchor_date = LAUNCH_ANCHORS[family]
    d = pd.Timestamp(anchor_date) - pd.DateOffset(months=6 * (anchor_series - series))
    return d + pd.Timedelta(days=(7 - d.weekday()) % 7 if d.weekday() >= 5 else 0)


def _fetch_index(conn, index_name, since):
    """Real Databricks rows only (has_data=True); the full grid is built later by _grid."""
    members = get_constituents(index_name)
    maturity = get_index_maturity(index_name).strftime('%Y-%m-%d')
    tickers = ', '.join(f"'{t}'" for t in sorted(members['Market_CDS_Ticker'].dropna().unique()))
    # No coupon filter: a name is quoted at one coupon only, which may differ from the index's (see coupon_matches_index)
    q = (f"SELECT {', '.join(SPREAD_COLS)} FROM {TABLE} "
         f"WHERE market_cds_ticker IN ({tickers}) AND cds_maturity = '{maturity}' AND published_date >= '{since}'")
    df = pd.read_sql(q, conn)
    df['published_date'] = pd.to_datetime(df['published_date'])
    df['cds_maturity'] = pd.to_datetime(df['cds_maturity'])
    df.insert(0, 'index_name', index_name)
    return df


def _grid(index_name, real, all_dates):
    """Exactly one row per index member per date, from the index's launch date to the latest table date.

    A member with no Databricks row on a date keeps its identity columns (index, member, ticker, date, maturity)
    and every data column is NaN; has_data tells the two apart. Members sharing a Market_CDS_Ticker each get the row.
    """
    members = get_constituents(index_name)[['member_name', 'Market_CDS_Ticker', 'markit_cds_ticker']].rename(columns={'Market_CDS_Ticker': 'market_cds_ticker'})
    maturity = get_index_maturity(index_name)
    ic = index_coupon(index_name)
    dates = all_dates[all_dates >= index_launch_date(index_name)]
    grid = members.merge(pd.DataFrame({'published_date': dates}), how='cross')
    grid['index_name'] = index_name
    grid['index_coupon'] = ic
    grid['cds_maturity'] = maturity
    data = real[[c for c in SPREAD_COLS if c != 'cds_maturity']].drop_duplicates(['market_cds_ticker', 'published_date', 'running_coupon'])
    out = grid.merge(data, on=['market_cds_ticker', 'published_date'], how='left')
    out['has_data'] = out['running_coupon'].notna()
    out['coupon_matches_index'] = (out['running_coupon'] == ic).where(out['has_data']).astype('boolean')
    return out[STORE_COLS]


def update_store(index_names=None, rebuild=False):
    """Build the local store, or add only the dates it doesn't have yet.

    Per index, fetches published_date >= (latest real date - REPULL_DAYS), replaces the overlapping rows, then
    rebuilds that index's full members x dates grid (missing names become all-NaN rows, has_data=False).
    rebuild=True ignores what is stored and pulls full history.
    """
    CACHE_DIR.mkdir(exist_ok=True)
    names = list(index_names) if index_names else list(load_index_list()['index_name'])
    store = pd.read_parquet(STORE_FILE) if STORE_FILE.exists() and not rebuild else None
    kept = [] if store is None else [store[~store['index_name'].isin(names)]]
    conn = get_conn()
    try:
        all_dates = pd.to_datetime(pd.read_sql(
            f"SELECT DISTINCT published_date FROM {TABLE} WHERE published_date >= '{HISTORY_START}'", conn)['published_date']).sort_values()
        for n in names:
            old = pd.DataFrame(columns=['index_name'] + SPREAD_COLS).astype({'published_date': 'datetime64[ns]'})
            since = max(index_launch_date(n), pd.Timestamp(HISTORY_START)).strftime('%Y-%m-%d')
            if store is not None:
                old = store[(store['index_name'] == n) & store['has_data']].drop(columns=['has_data', 'coupon_matches_index'])
                if not old.empty:
                    since = (old['published_date'].max() - pd.Timedelta(days=REPULL_DAYS)).strftime('%Y-%m-%d')
            new = _fetch_index(conn, n, since)
            real = pd.concat([old[old['published_date'] < pd.Timestamp(since)].drop(columns=['index_coupon', 'member_name'], errors='ignore'), new],
                             ignore_index=True)
            if real.empty:
                print(f"{n}: no Databricks data at maturity {get_index_maturity(n):%Y-%m-%d}; skipped")
                continue
            g = _grid(n, real, all_dates)
            print(f"{n}: {len(new):,} rows pulled since {since}; grid {len(g):,} rows, {(~g['has_data']).sum():,} missing")
            kept.append(g)
    finally:
        conn.close()
    out = pd.concat(kept, ignore_index=True)
    key = ['index_name', 'member_name', 'market_cds_ticker', 'published_date', 'running_coupon']
    dup = out.duplicated(key, keep=False)
    if dup.any():
        raise ValueError(f"{dup.sum()} duplicate rows on {key}:\n{out[dup].sort_values(key).head(10)}")
    out = out[STORE_COLS].sort_values(['index_name', 'member_name', 'published_date']).reset_index(drop=True)
    tmp = STORE_FILE.with_suffix('.tmp')
    out.to_parquet(tmp, index=False)
    tmp.replace(STORE_FILE)
    print(f"store: {len(out):,} rows, through {out['published_date'].max():%Y-%m-%d}")
    return out
