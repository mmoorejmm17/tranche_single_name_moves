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

ROOT = Path(__file__).resolve().parent.parent      # project root (this file lives in src/)
INDEX_FILE = ROOT / 'config' / 'cds_index.csv'
CACHE_DIR = ROOT / 'cache'
TABLE = 'teams.structured_credit_abhutra.cds_transform_interpolated'
SPREAD_COLS = ['published_date', 'market_cds_ticker', 'cds_maturity', 'batch', 'doc_clause', 'currency',
               'running_coupon', 'par_spread', 'conv_spread', 'upfront', 'cds_assumed_recovery', 'risky_pv01', 'default_probability',
               'all_upfront']
_ID_COLS = ['index_name', 'index_coupon', 'member_name', 'market_cds_ticker', 'markit_cds_ticker', 'published_date', 'has_data',
            'coupon_matches_index']
STORE_COLS = _ID_COLS + [c for c in SPREAD_COLS if c not in _ID_COLS]   # column order of spreads.parquet
STORE_FILE = CACHE_DIR / 'spreads.parquet'        # raw Databricks mirror, one row per index x member x date (full grid)
OVERRIDES_FILE = ROOT / 'config' / 'overrides.csv'           # hand-maintained fixes, never touched by refreshes
OVERRIDE_KEY = ['market_cds_ticker', 'published_date', 'cds_maturity']
# DEFINITION of PRICED lives in priced_mask(): a par_spread, or a conv_spread (> 0) / upfront with its running_coupon (risky_pv01 optional; see CLAUDE.md).
EXTRA_COLS = ['upfront', 'all_upfront', 'risky_pv01']   # shown next to the spread at dates X and Y
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


RECAL_FILE = CACHE_DIR / 'recalibrated.parquet'   # written by cds_recalib.py (needs the Quantifi Toolkit; this module does not)
RECAL_KEY = ['market_cds_ticker', 'published_date', 'cds_maturity', 'running_coupon']
PRICED_FILE = CACHE_DIR / 'priced_at_index_coupon.parquet'   # written by cds_pricer.py (also needs the Toolkit): every priced point re-priced at its index coupon
PRICED_KEY = ['market_cds_ticker', 'published_date', 'cds_maturity', 'running_coupon', 'index_coupon']
ADDIN_FILE = CACHE_DIR / 'addin_fill.parquet'     # written by addin_fill.py: cells Databricks could not price, filled from the Markit Excel add-in at the index coupon
ADDIN_KEY = ['index_name', 'market_cds_ticker', 'published_date', 'cds_maturity']


def _load_recal():
    """Recalibrated conv_spread / risky_pv01 for rows where Markit's conv_spread is 0, or None if cds_recalib.py hasn't been run."""
    if not RECAL_FILE.exists():
        return None
    rc = pd.read_parquet(RECAL_FILE)
    return rc[rc['recal_status'].str.startswith('ok')]


def _load_priced():
    """Every priced point re-priced at its index coupon (cds_pricer.price_store), or None if that hasn't been run."""
    return pd.read_parquet(PRICED_FILE) if PRICED_FILE.exists() else None


def _load_addin():
    """Cells filled from the Markit add-in (addin_fill.py), or None if that has not been run."""
    return pd.read_parquet(ADDIN_FILE) if ADDIN_FILE.exists() else None


def priced_mask(df):
    """PRICED by the agreed definition: a par_spread (> 0), or a conv_spread (> 0) / upfront together with its running_coupon.
    risky_pv01 is optional (cds_pricer derives it from a flat curve). conv_spread == 0 is Markit's 'no spread solves' placeholder, not a price."""
    par = df['par_spread'].notna() & (df['par_spread'] > 0)
    quote = df['running_coupon'].notna() & (df['upfront'].notna() | (df['conv_spread'].notna() & (df['conv_spread'] > 0)))
    return df['has_data'] & (par | quote)


def spread_changes(index_name, date_x, date_y, metric='conv_spread'):
    """One row per non-zero-weight member: `metric` at date X, at date Y, and the change (Y - X), widest first.

    Dates snap to the last available published_date on or before the request. Members with no data on a date keep their row with
    NaN; x_state / y_state / status say why. upfront, all_upfront and risky_pv01 at X and Y, and their changes, ride along.

    With cache/priced_at_index_coupon.parquet (cds_pricer.py) every conv_spread and its upfront / all_upfront / risky_pv01 are shown AT THE
    INDEX COUPON: states 'ok' (quoted at the index coupon), 'recoupon' (quoted at the other coupon, re-priced), 'recal' (a wide name whose
    conv_spread only solves at a marked-down recovery; recovery_x / recovery_y say what was used). Without that file, a Markit conv_spread of
    0 is 'recal' if cds_recalib.py has been run, else 'conv=0'. 'missing' = no Databricks row, 'incomplete' = a row with nothing priced.
    With cache/addin_fill.parquet (addin_fill.py), a cell Databricks could not price ('missing' / 'incomplete') takes the Markit add-in values: state 'addin'.
    A manual spread_override (overrides.csv) beats all of these ('override').
    """
    members = get_constituents(index_name)
    hist = get_spread_history(index_name)
    dates = hist['published_date'].drop_duplicates()
    dx, dy = _asof(dates, date_x), _asof(dates, date_y)

    h = hist.copy()
    h['recovery_used'] = float('nan')
    h['value'] = pd.to_numeric(h[metric], errors='coerce')
    h['state'] = 'missing'
    h.loc[h['has_data'], 'state'] = 'ok'
    h.loc[h['has_data'] & ~priced_mask(h), 'state'] = 'incomplete'      # a row exists but nothing is priced (stub)
    if metric == 'conv_spread':
        pr = _load_priced()
        if pr is not None:
            ic = ['conv_spread', 'upfront', 'all_upfront', 'risky_pv01']
            pr = pr[PRICED_KEY + ic + ['conv_status', 'recovery_used']].rename(columns={**{c: c + '_ic' for c in ic}, 'recovery_used': 'rec_used'})
            h = h.merge(pr, on=PRICED_KEY, how='left')
            fix = h['conv_spread_ic'].notna()
            for c in ic:
                h.loc[fix, c if c != 'conv_spread' else 'value'] = h.loc[fix, c + '_ic']
            own = h['running_coupon'] == h['index_coupon']
            h.loc[fix, 'state'] = 'recoupon'
            h.loc[fix & own, 'state'] = 'ok'
            wide = fix & h['conv_status'].fillna('').str.startswith('recal')
            h.loc[wide, 'state'] = 'recal'
            h.loc[wide, 'recovery_used'] = h.loc[wide, 'rec_used']
        else:
            zero = h['has_data'] & (h['value'] == 0)
            h.loc[zero, ['value', 'state']] = [float('nan'), 'conv=0']
            rc = _load_recal()
            if rc is not None:
                h = h.merge(rc[RECAL_KEY + ['conv_spread_recal', 'risky_pv01_recal', 'recovery_used']].rename(columns={'recovery_used': 'rec_used'}),
                            on=RECAL_KEY, how='left')
                fix = (h['state'] == 'conv=0') & h['conv_spread_recal'].notna()
                h.loc[fix, 'value'] = h.loc[fix, 'conv_spread_recal']
                h.loc[fix, 'risky_pv01'] = h.loc[fix, 'risky_pv01_recal']
                h.loc[fix, 'recovery_used'] = h.loc[fix, 'rec_used']
                h.loc[fix, 'state'] = 'recal'
        ad = _load_addin()
        if ad is not None:        # waterfall: Databricks first; a cell it could not price takes the add-in values; overrides still win below
            cols = ['conv_spread', 'upfront', 'all_upfront', 'risky_pv01']
            h = h.merge(ad[ADDIN_KEY + cols].rename(columns={c: c + '_ad' for c in cols}), on=ADDIN_KEY, how='left')
            gap = h['state'].isin(['missing', 'incomplete']) & h['conv_spread_ad'].notna()
            h.loc[gap, 'value'] = h.loc[gap, 'conv_spread_ad']
            for c in ('upfront', 'all_upfront', 'risky_pv01'):
                h.loc[gap, c] = h.loc[gap, c + '_ad']
            h.loc[gap, 'state'] = 'addin'
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
    for col in EXTRA_COLS:   # at the index coupon when the pricer file exists, else as stored; NaN where the name has no row
        if col != metric:
            out[f'{col}_x'] = t.map(pd.to_numeric(px[col], errors='coerce'))
            out[f'{col}_y'] = t.map(pd.to_numeric(py[col], errors='coerce'))
            out[f'{col}_change'] = out[f'{col}_y'] - out[f'{col}_x']
    out['recovery_x'] = t.map(px['recovery_used'])
    out['recovery_y'] = t.map(py['recovery_used'])
    out['quoted_coupon_x'] = t.map(px['running_coupon'])      # the coupon Markit quoted at (differs from the index coupon on 'recoupon' rows)
    out['quoted_coupon_y'] = t.map(py['running_coupon'])
    out['x_state'] = t.map(px['state']).fillna('missing')
    out['y_state'] = t.map(py['state']).fillna('missing')
    bad = {'missing', 'incomplete', 'conv=0', 'recal', 'recoupon', 'addin'}
    out['status'] = 'ok'
    for side in ('x', 'y'):
        flag = out[f'{side}_state'].isin(bad)
        out.loc[flag, 'status'] = out.loc[flag, 'status'].replace('ok', '') + out.loc[flag, f'{side}_state'].radd(f'{side.upper()} ') + '; '
    out['status'] = out['status'].str.rstrip('; ')
    out['y_batch'] = t.map(py['batch'])  # EOD-Interp = interpolated, not a quoted tenor
    out['in_databricks'] = t.isin(hist.loc[hist['has_data'], 'market_cds_ticker'])
    out.attrs.update(index_name=index_name, date_x=dx, date_y=dy, maturity=get_index_maturity(index_name), metric=metric,
                     index_coupon=index_coupon(index_name))
    return out.sort_values('change', ascending=False, na_position='last').reset_index(drop=True)


WHAT_NEVER = 'Never priced in this index'
WHAT_CONV0 = 'conv=0 (no conventional spread solves)'
WHAT_INCOMPLETE = 'Incomplete (row exists but nothing is priced)'
WHAT_GAP = 'Gap (has data on other dates)'
WHAT_RECAL = 'Priced, wide name: conv_spread at a marked-down recovery'
WHAT_RECOUPON = 'Priced, re-priced from the other coupon to the index coupon'
WHAT_ADDIN = 'Priced from the Markit add-in (Databricks has no price)'
_WHAT_ORDER = [WHAT_NEVER, WHAT_CONV0, WHAT_INCOMPLETE, WHAT_GAP, WHAT_RECAL, WHAT_RECOUPON, WHAT_ADDIN]
_BAD_PATTERN = 'missing|incomplete|conv=0'      # a status containing one of these means no price on that date; 'recal' / 'recoupon' alone are priced


def priced_counts(res):
    """(priced at the index coupon, re-priced from the other coupon, wide name via marked-down recovery, priced from the Markit add-in, not priced)
    member counts. A name counts as not priced if it is on either date; otherwise the most derived source on either date wins."""
    st = res['status']
    unpriced = st.str.contains(_BAD_PATTERN)
    addin = st.str.contains('addin') & ~unpriced
    recal = st.str.contains('recal') & ~unpriced & ~addin
    recoupon = st.str.contains('recoupon') & ~unpriced & ~addin & ~recal
    own = ~unpriced & ~addin & ~recal & ~recoupon
    return tuple(int(x.sum()) for x in (own, recoupon, recal, addin, unpriced))


def missing_report(res):
    """Members flagged by spread_changes() (status != 'ok'), with how much data each has over the index's life.

    Columns: member_name, Market_CDS_Ticker, what, status, days_with_data, days_in_index, first_data, quoted coupon X / Y, recovery X / Y (%).
    'what' is why: never priced in this index, conv=0, incomplete (stub row), a gap, priced as a wide name (recovery marked down), or priced
    after re-pricing from the other coupon. The last two kinds count as priced.
    """
    idx = res.attrs['index_name']
    hist = get_spread_history(idx)
    days_in_index = hist['published_date'].nunique()
    real = hist.loc[priced_mask(hist), ['market_cds_ticker', 'published_date']]
    ad = _load_addin()
    if ad is not None:      # days priced by either source
        real = pd.concat([real, ad.loc[ad['index_name'] == idx, ['market_cds_ticker', 'published_date']]], ignore_index=True)
    cov = pd.DataFrame({'days_with_data': real.groupby('market_cds_ticker')['published_date'].nunique(),
                        'first_data': real.groupby('market_cds_ticker')['published_date'].min()})
    flagged = res.loc[res['status'] != 'ok', ['member_name', 'Market_CDS_Ticker', 'status', 'quoted_coupon_x', 'quoted_coupon_y',
                                              'recovery_x', 'recovery_y']].copy()
    flagged = flagged.join(cov, on='Market_CDS_Ticker')
    flagged['days_with_data'] = flagged['days_with_data'].fillna(0).astype(int)
    flagged['days_in_index'] = days_in_index
    st = flagged['status']
    flagged['what'] = WHAT_GAP
    flagged.loc[st.str.contains('incomplete'), 'what'] = WHAT_INCOMPLETE
    flagged.loc[st.str.contains('conv=0'), 'what'] = WHAT_CONV0
    flagged.loc[flagged['days_with_data'] == 0, 'what'] = WHAT_NEVER
    flagged.loc[~st.str.contains(_BAD_PATTERN), 'what'] = WHAT_RECOUPON
    flagged.loc[~st.str.contains(_BAD_PATTERN) & st.str.contains('recal'), 'what'] = WHAT_RECAL
    flagged.loc[~st.str.contains(_BAD_PATTERN) & st.str.contains('addin'), 'what'] = WHAT_ADDIN
    flagged['what'] = pd.Categorical(flagged['what'], categories=_WHAT_ORDER, ordered=True)
    flagged = flagged.sort_values(['what', 'member_name']).reset_index(drop=True)
    flagged = flagged.rename(columns={'quoted_coupon_x': 'quoted coupon X', 'quoted_coupon_y': 'quoted coupon Y',
                                      'recovery_x': 'recovery X (%)', 'recovery_y': 'recovery Y (%)'})
    return flagged[['member_name', 'Market_CDS_Ticker', 'what', 'status', 'days_with_data', 'days_in_index', 'first_data',
                    'quoted coupon X', 'quoted coupon Y', 'recovery X (%)', 'recovery Y (%)']]


def missing_banner_html(res):
    """Boxed HTML summary: names with no price (red, grouped by reason), wide names priced at a marked-down recovery (amber) and names
    re-priced from the other coupon (blue). Display with IPython.display.HTML."""
    import html as _html
    a = res.attrs
    flagged = missing_report(res)
    when = f"{a['date_x']:%Y-%m-%d} to {a['date_y']:%Y-%m-%d}"
    miss = flagged[flagged['what'].isin(_WHAT_ORDER[:4])]
    recal = flagged[flagged['what'] == WHAT_RECAL]
    recoup = flagged[flagged['what'] == WHAT_RECOUPON]
    addin = flagged[flagged['what'] == WHAT_ADDIN]
    ic = a.get('index_coupon')
    parts = []
    if miss.empty:
        parts.append(f'<div style="border:2px solid #2e7d32;background:#e8f5e9;color:#0b0b0b;padding:10px 14px;border-radius:6px">'
                     f'All {len(res)} names are priced on both dates ({a["index_name"]}, {when}).</div>')
    else:
        parts.append(f'<div style="border:2px solid #c0392b;background:#fdecea;color:#0b0b0b;padding:10px 14px;border-radius:6px;line-height:1.6">'
                     f'<b style="font-size:110%">&#9888; {len(miss)} of {len(res)} names are not priced</b> '
                     f'({a["index_name"]}, {when}; priced = a par_spread, or a conv_spread / upfront with its coupon)')
        for what in _WHAT_ORDER[:4]:
            g = miss[miss['what'] == what]
            if g.empty:
                continue
            names = ', '.join(_html.escape(f"{r.member_name} ({r.Market_CDS_Ticker})") + (f" [{r.status}]" if what == WHAT_GAP else '')
                              for r in g.itertuples())
            parts.append(f'<div><b>{_html.escape(what)} ({len(g)}):</b> {names}</div>')
        parts.append('</div>')
    if not recal.empty:
        def rec(r):
            vals = [f'{v:.0f}%' for v in (r._10, r._11) if pd.notna(v)]
            return f' [recovery {" / ".join(vals)}]' if vals else ''
        names = ', '.join(_html.escape(f"{r.member_name} ({r.Market_CDS_Ticker})") + rec(r) for r in recal.itertuples())
        parts.append(f'<div style="border:2px solid #e08a00;background:#fff4e0;color:#0b0b0b;padding:10px 14px;border-radius:6px;margin-top:8px;line-height:1.6">'
                     f'<b>&#9888; {len(recal)} wide names priced with a marked-down recovery</b> '
                     f'(no conventional spread solves at 40%; the spread is a clean comparable mark, not Markit\'s): {names}</div>')
    if not recoup.empty:
        def cpn(r):
            vals = sorted({int(v) for v in (r._8, r._9) if pd.notna(v)})
            return f' [quoted at {"/".join(map(str, vals))}]' if vals else ''
        names = ', '.join(_html.escape(f"{r.member_name} ({r.Market_CDS_Ticker})") + cpn(r) for r in recoup.itertuples())
        parts.append(f'<div style="border:2px solid #3b7bbf;background:#eaf2fb;color:#0b0b0b;padding:10px 14px;border-radius:6px;margin-top:8px;line-height:1.6">'
                     f'<b>&#8505; {len(recoup)} names re-priced to the index coupon{f" ({ic})" if ic else ""}</b> '
                     f'(Markit quotes them at the other coupon; spreads, upfronts and risky_pv01 shown are re-priced with cds_pricer): {names}</div>')
    if not addin.empty:
        names = ', '.join(_html.escape(f"{r.member_name} ({r.Market_CDS_Ticker})") for r in addin.itertuples())
        parts.append(f'<div style="border:2px solid #7e57c2;background:#f3edfa;color:#0b0b0b;padding:10px 14px;border-radius:6px;margin-top:8px;line-height:1.6">'
                     f'<b>&#8505; {len(addin)} names priced from the Markit add-in</b> '
                     f'(Databricks has no price on at least one of the dates; values come straight from Markit at the index coupon, interpolated between annual tenors where needed): {names}</div>')
    return ''.join(parts)


def pricing_coverage(index_names=None):
    """Scoreboard of how much of the store is PRICED by the agreed definition, one row per index plus a total.

    Cells are index x member x date. Columns: cells, priced (Databricks, by priced_mask), priced %, quoted at index coupon % (the rest are priced at the name's
    other coupon and need cds_pricer to be shown at the index coupon), the cells Databricks could not price, how many of those the Markit add-in fills
    (addin_fill.py) and what is still not priced; 'conv=0 (priced via par)' counts wide-name rows that Markit gives no conv_spread but which still count as priced.
    """
    cols = ['index_name', 'market_cds_ticker', 'published_date', 'cds_maturity', 'has_data', 'coupon_matches_index', 'running_coupon', 'par_spread', 'upfront', 'conv_spread']
    st = pd.read_parquet(STORE_FILE, columns=cols)
    if index_names is not None:
        st = st[st['index_name'].isin(list(index_names))]
    st['priced'] = priced_mask(st)
    st['own_coupon'] = st['priced'] & (st['coupon_matches_index'] == True)
    st['no_row'] = ~st['has_data']
    st['empty_row'] = st['has_data'] & ~st['priced']
    st['conv0'] = st['priced'] & (st['conv_spread'] == 0)
    st['addin'] = False
    ad = _load_addin()
    if ad is not None:
        st = st.drop(columns='addin').merge(ad[ADDIN_KEY].assign(addin=True), on=ADDIN_KEY, how='left')
        st['addin'] = st['addin'].fillna(False).astype(bool) & ~st['priced']
    g = st.groupby('index_name').agg(cells=('priced', 'size'), priced=('priced', 'sum'), own_coupon=('own_coupon', 'sum'), addin=('addin', 'sum'),
                                    no_row=('no_row', 'sum'), empty_row=('empty_row', 'sum'), conv0=('conv0', 'sum'))
    g = g.loc[[i for i in load_index_list()['index_name'] if i in g.index]]
    g.loc['TOTAL'] = g.sum()
    out = pd.DataFrame({'cells': g['cells'], 'priced': g['priced'], 'priced %': g['priced'] / g['cells'] * 100,
                        'quoted at index coupon %': g['own_coupon'] / g['cells'] * 100, 'not priced': g['cells'] - g['priced'],
                        'filled from add-in': g['addin'], 'priced % incl. add-in': (g['priced'] + g['addin']) / g['cells'] * 100,
                        'still not priced': g['cells'] - g['priced'] - g['addin'], 'no row': g['no_row'], 'empty row': g['empty_row'],
                        'conv=0 (priced via par)': g['conv0']})
    ints = ('cells', 'priced', 'not priced', 'filled from add-in', 'still not priced', 'no row', 'empty row', 'conv=0 (priced via par)')
    return out.astype({c: int for c in ints}).rename_axis('index').reset_index()


def show_table(df, numeric=(), flag_col=None, decimals=1, align='center'):
    """HTML table for notebooks: nothing wraps, `numeric` columns always show a fixed number of decimals (`decimals`, or a
    {column: decimals} dict), missing values are blank, dates show as YYYY-MM-DD. Rows where `flag_col` is not 'ok' are shaded:
    red and bold if it says missing / incomplete / conv=0, amber and bold if it says recal, purple if it says addin, blue if it only says recoupon.
    align='center' (default) centers every cell and header; align='auto' right-aligns the numeric columns and left-aligns the rest.
    Plain rows (and the header) are white on black; tinted rows are black on a light tint, so every cell has an explicit, readable text and background color in any notebook theme.
    Plain HTML, so it needs no extra packages. Display it by making it the last expression of a cell."""
    import html as _html
    from IPython.display import HTML
    places = numeric if isinstance(numeric, dict) else {c: decimals for c in numeric}
    al = (lambda c: 'center') if align == 'center' else (lambda c: 'right' if c in places else 'left')

    def cell(col, v):
        if v is None or (not isinstance(v, str) and pd.isna(v)):
            return ''
        if col in places:
            return f'{v:.{places[col]}f}'
        if isinstance(v, pd.Timestamp):
            return f'{v:%Y-%m-%d}'
        return _html.escape(str(v))

    th = 'style="text-align:{a};white-space:nowrap;padding:3px 10px;border-bottom:2px solid #666;color:#ffffff;background:#000000"'
    head = ''.join(f'<th {th.format(a=al(c))}>{_html.escape(str(c))}</th>' for c in df.columns)
    rows = []
    for rec in df.to_dict('records'):
        status = str(rec[flag_col]) if flag_col in df.columns else 'ok'
        ink = '#ffffff' if status == 'ok' else '#0b0b0b'      # plain rows: white on black; tinted rows: black on the light tint
        if status == 'ok':
            tr = ' style="background:#000000;color:#ffffff"'
        elif any(w in status for w in ('missing', 'incomplete', 'conv=0')):
            tr = ' style="background:#fdecea;color:#0b0b0b;font-weight:bold"'
        elif 'recal' in status:
            tr = ' style="background:#fff4e0;color:#0b0b0b;font-weight:bold"'
        elif 'addin' in status:
            tr = ' style="background:#f3edfa;color:#0b0b0b"'
        else:
            tr = ' style="background:#eaf2fb;color:#0b0b0b"'
        tds = ''.join(f'<td style="text-align:{al(c)};white-space:nowrap;padding:2px 10px;color:{ink}">{cell(c, rec[c])}</td>'
                      for c in df.columns)
        rows.append(f'<tr{tr}>{tds}</tr>')
    return HTML(f'<table style="border-collapse:collapse;font-size:90%;color:#ffffff;background:#000000"><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table>')
