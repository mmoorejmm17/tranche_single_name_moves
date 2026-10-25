"""Single-name CDS spread moves for the constituents of a CDX / iTraxx index.

Flow: index_name (e.g. 'IG47') -> bbg ticker (cds_index.csv) -> non-zero-weight members with
Market_CDS_Ticker (cdx_members) -> index MATURITY via BDP -> Databricks rows at that
cds_maturity (local parquet store, updated incrementally) -> spread change between two dates.
"""
import sys
from pathlib import Path

import pandas as pd

# own utils: not pip-installed, so add the repo roots to sys.path
for _p in ('S:/Structured Credit/Matt Stuff/claude_repos/bbg_utils',
           'S:/Structured Credit/Matt Stuff/claude_repos/databricks_utils'):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from bbg_utils import bdp_mm
from databricks_utils._connection import get_conn

from cdx_members import get_index_members

ROOT = Path(__file__).parent
INDEX_FILE = ROOT / 'cds_index.csv'
CACHE_DIR = ROOT / 'cache'
TABLE = 'teams.structured_credit_abhutra.cds_transform_interpolated'
SPREAD_COLS = ['published_date', 'market_cds_ticker', 'cds_maturity', 'batch', 'doc_clause', 'currency',
               'running_coupon', 'par_spread', 'conv_spread', 'upfront', 'cds_assumed_recovery', 'risky_pv01', 'default_probability',
               'all_upfront']
_ID_COLS = ['index_name', 'index_coupon', 'member_name', 'market_cds_ticker', 'markit_cds_ticker', 'published_date', 'has_data',
            'coupon_matches_index']
STORE_COLS = _ID_COLS + [c for c in SPREAD_COLS if c not in _ID_COLS]   # column order of spreads.parquet
STORE_FILE = CACHE_DIR / 'spreads.parquet'        # raw Databricks mirror, one row per index x member x date (full grid)
OVERRIDES_FILE = ROOT / 'overrides.csv'           # hand-maintained fixes, never touched by refreshes
OVERRIDE_KEY = ['market_cds_ticker', 'published_date', 'cds_maturity']
REPULL_DAYS = 7                                   # re-fetch this far back on update in case Databricks restated
HISTORY_START = '2022-04-01'  # table starts here


def load_index_list():
    return pd.read_csv(INDEX_FILE)


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


def index_coupon(index_name):
    """Standard running coupon in bp: HY and XO trade at 500, IG and MA at 100."""
    return 500 if index_name[:2] in ('HY', 'XO') else 100


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


def load_overrides():
    """overrides.csv columns: market_cds_ticker, published_date, cds_maturity, spread_override, note."""
    if not OVERRIDES_FILE.exists():
        return pd.DataFrame(columns=OVERRIDE_KEY + ['spread_override', 'note'])
    o = pd.read_csv(OVERRIDES_FILE, parse_dates=['published_date', 'cds_maturity'])
    if o.duplicated(OVERRIDE_KEY).any():
        raise ValueError(f"duplicate keys in {OVERRIDES_FILE.name}: {o[o.duplicated(OVERRIDE_KEY, keep=False)]}")
    return o


def get_spread_history(index_name):
    """Stored rows for one index (all dates) with spread_override / note joined on from overrides.csv.

    Run update_store() to build or refresh the store; this only reads it.
    """
    if not STORE_FILE.exists():
        raise FileNotFoundError(f"{STORE_FILE} missing; run update_store() first")
    df = pd.read_parquet(STORE_FILE, filters=[('index_name', '==', index_name)])
    if df.empty:
        raise KeyError(f"{index_name!r} not in store; run update_store([{index_name!r}])")
    df = df.merge(load_overrides(), on=OVERRIDE_KEY, how='left')
    dup = df.duplicated(['member_name', 'market_cds_ticker', 'published_date'], keep=False)
    if dup.any():
        raise ValueError(f"{dup.sum()} duplicate (ticker, date) rows for {index_name}; a name changed coupon on one date:\n"
                         f"{df[dup].sort_values(['member_name', 'published_date']).head(10)}")
    return df


def _asof(dates, target):
    """Last available date <= target."""
    ok = dates[dates <= pd.Timestamp(target)]
    if ok.empty:
        raise ValueError(f"no data on or before {target}; earliest is {dates.min():%Y-%m-%d}")
    return ok.max()


def spread_changes(index_name, date_x, date_y, metric='conv_spread'):
    """One row per non-zero-weight member: `metric` at date X, at date Y, and the change (Y - X), widest first.

    Dates snap to the last available published_date on or before the request. Members with no data on a date keep
    their row with NaN; x_state / y_state / status say why. For conv_spread a Markit value of 0 means no spread
    solves (wide names) and is treated as missing, and a manual spread_override (overrides.csv) replaces the value.
    States: 'ok' (Databricks), 'override', 'missing' (no row), 'conv=0' (zero conv_spread, no override).
    """
    members = get_constituents(index_name)
    hist = get_spread_history(index_name)
    dates = hist['published_date'].drop_duplicates()
    dx, dy = _asof(dates, date_x), _asof(dates, date_y)

    h = hist.copy()
    h['value'] = pd.to_numeric(h[metric], errors='coerce')
    h['state'] = 'missing'
    h.loc[h['has_data'], 'state'] = 'ok'
    if metric == 'conv_spread':
        zero = h['has_data'] & (h['value'] == 0)
        h.loc[zero, ['value', 'state']] = [float('nan'), 'conv=0']
        ovr = pd.to_numeric(h['spread_override'], errors='coerce')
        h.loc[ovr.notna(), 'value'] = ovr
        h.loc[ovr.notna(), 'state'] = 'override'
    h = h.drop_duplicates(['market_cds_ticker', 'published_date'])
    px = h.loc[h['published_date'] == dx].set_index('market_cds_ticker')
    py = h.loc[h['published_date'] == dy].set_index('market_cds_ticker')

    out = members[['member_name', 'Market_CDS_Ticker', 'weight', 'currency']].copy()
    t = out['Market_CDS_Ticker']
    out[f'{metric}_x'] = t.map(px['value'])
    out[f'{metric}_y'] = t.map(py['value'])
    out['change'] = out[f'{metric}_y'] - out[f'{metric}_x']
    out['pct_change'] = out['change'] / out[f'{metric}_x'] * 100
    out['x_state'] = t.map(px['state']).fillna('missing')
    out['y_state'] = t.map(py['state']).fillna('missing')
    bad = {'missing', 'conv=0'}
    out['status'] = 'ok'
    for side in ('x', 'y'):
        flag = out[f'{side}_state'].isin(bad)
        out.loc[flag, 'status'] = out.loc[flag, 'status'].replace('ok', '') + out.loc[flag, f'{side}_state'].radd(f'{side.upper()} ') + '; '
    out['status'] = out['status'].str.rstrip('; ')
    out['y_batch'] = t.map(py['batch'])  # EOD-Interp = interpolated, not a quoted tenor
    out['in_databricks'] = t.isin(hist.loc[hist['has_data'], 'market_cds_ticker'])
    out.attrs.update(index_name=index_name, date_x=dx, date_y=dy, maturity=get_index_maturity(index_name), metric=metric)
    return out.sort_values('change', ascending=False, na_position='last').reset_index(drop=True)
