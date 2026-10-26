"""Single-name CDS spread moves for the constituents of a CDX / iTraxx index (READ side: analysis of the published data/ snapshot).

Flow: index_name (e.g. 'IG47') -> non-zero-weight members and the index maturity (published rosters) -> store rows at that
cds_maturity -> spread change between two dates. The store is BUILT by pricing/ (cdx_store.update_store, then publish.py); nothing here writes it.
"""
import json
import os
from pathlib import Path

import pandas as pd

# READ SIDE. Everything in analysis/ only READS the published snapshot in data/ (written by pricing/shared/publish.py, never by the notebooks).
# No Bloomberg / Databricks / Quantifi / Excel is needed to import or use it. The pricing scripts point these two env vars at their working
# copy (cache/ and config/) before importing this module (pricing/shared/_paths.py); otherwise the defaults below apply.
ROOT = Path(__file__).resolve().parent.parent      # project root (this file lives in analysis/)
STORE = os.environ.get('CDS_STORE') or 'base'          # which store: a folder with prices/ reference/ metrics/ MANIFEST.json (today only 'base' exists)
SHARED_DATA_DIR = Path('S:/Structured Credit/Matt Stuff/tranche_single_name_moves/data') / STORE   # the owner's published store on the shared drive


def _default_data_dir():
    """CDS_DATA_DIR if set; else this checkout's data/ if it holds a published snapshot; else the shared-drive snapshot (a fresh GitHub clone has no data/)."""
    if os.environ.get('CDS_DATA_DIR'):
        return Path(os.environ['CDS_DATA_DIR'])
    return ROOT / 'data' / STORE if (ROOT / 'data' / STORE / 'MANIFEST.json').exists() else SHARED_DATA_DIR


DATA_DIR = _default_data_dir()                       # the store root
PRICES_DIR, REF_DIR, METRICS_DIR = DATA_DIR / 'prices', DATA_DIR / 'reference', DATA_DIR / 'metrics'
CONFIG_DIR = Path(os.environ.get('CDS_CONFIG_DIR') or REF_DIR)   # hand-maintained inputs: cds_index.csv, overrides.csv (copied into reference/ on publish)
INDEX_FILE = CONFIG_DIR / 'cds_index.csv'
SPREAD_COLS = ['published_date', 'market_cds_ticker', 'cds_maturity', 'batch', 'doc_clause', 'currency',
               'running_coupon', 'par_spread', 'conv_spread', 'upfront', 'cds_assumed_recovery', 'risky_pv01', 'default_probability',
               'all_upfront']
_ID_COLS = ['index_name', 'index_coupon', 'member_name', 'market_cds_ticker', 'markit_cds_ticker', 'published_date', 'has_data',
            'coupon_matches_index']
STORE_COLS = _ID_COLS + [c for c in SPREAD_COLS if c not in _ID_COLS]   # column order of spreads.parquet
STORE_FILE = PRICES_DIR / 'spreads.parquet'        # raw Databricks mirror, one row per index x member x date (full grid)
OVERRIDES_FILE = CONFIG_DIR / 'overrides.csv'                # hand-maintained ballpark conv_spreads (pricing/fix_history/overrides.py prices them into overrides_priced.parquet)
MANUAL_FILE = PRICES_DIR / 'overrides_priced.parquet'          # the priced overlay of those overrides: wins over every other source for the cells it lists
# DEFINITION of PRICED lives in priced_mask(): a par_spread, or a conv_spread (> 0) / upfront with its running_coupon (risky_pv01 optional; see CLAUDE.md).
EXTRA_COLS = ['upfront', 'all_upfront', 'risky_pv01']   # shown next to the spread at dates X and Y


def load_index_list():
    return pd.read_csv(INDEX_FILE)


def data_as_of():
    """What the published snapshot is: {'published_at', 'store_through', 'files': {...}} from data/MANIFEST.json, or None if nothing was published."""
    p = DATA_DIR / 'MANIFEST.json'
    return json.loads(p.read_text(encoding='utf-8')) if p.exists() else None


def print_data_as_of():
    m = data_as_of()
    print('no published data (data/MANIFEST.json missing)' if m is None else f"data published {m['published_at']}; prices through {m['store_through']}")


def get_index_maturity(index_name):
    """The index's 5Y maturity date (static per series), read from the published reference/index_maturity.csv."""
    path = REF_DIR / 'index_maturity.csv'
    if not path.exists():
        raise FileNotFoundError(f'{path} missing: no published data')
    cache = pd.read_csv(path, index_col='index_name')['maturity']
    if index_name not in cache.index:
        raise KeyError(f'{index_name!r} not in the published data')
    return pd.Timestamp(cache[index_name])


def get_constituents(index_name):
    """Non-zero-weight members of a series (zero weight = defaulted, no longer in the index), read from the published reference/members/members_<index>.csv."""
    path = REF_DIR / 'members' / f'members_{index_name}.csv'
    if not path.exists():
        raise FileNotFoundError(f'{path} missing: no published data for {index_name}')
    return pd.read_csv(path, dtype=str).assign(weight=lambda d: pd.to_numeric(d['weight']))


def index_coupon(index_name):
    """Standard running coupon in bp: HY and XO trade at 500, IG and MA at 100."""
    return 500 if index_name[:2] in ('HY', 'XO') else 100


def get_spread_history(index_name):
    """Stored rows for one index (all dates).

    This only reads the published store (pricing/shared/publish.py writes it).
    """
    if not STORE_FILE.exists():
        raise FileNotFoundError(f"{STORE_FILE} missing: no published data")
    df = pd.read_parquet(STORE_FILE, filters=[('index_name', '==', index_name)])
    if df.empty:
        raise KeyError(f"{index_name!r} not in the published store")
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


RECAL_FILE = PRICES_DIR / 'recalibrated.parquet'   # written by cds_recalib.py (needs the Quantifi Toolkit; this module does not)
RECAL_KEY = ['market_cds_ticker', 'published_date', 'cds_maturity', 'running_coupon']
PRICED_FILE = PRICES_DIR / 'priced_at_index_coupon.parquet'   # written by cds_pricer.py (also needs the Toolkit): every priced point re-priced at its index coupon
PRICED_KEY = ['market_cds_ticker', 'published_date', 'cds_maturity', 'running_coupon', 'index_coupon']
ADDIN_FILE = PRICES_DIR / 'addin_fill.parquet'     # written by addin_fill.py: cells Databricks could not price, filled from the Markit Excel add-in at the index coupon
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


def _load_manual():
    """Manually overridden cells, priced by Quantifi from the owner's ballpark conv_spreads (pricing/fix_history/overrides.py), or None if there are none."""
    return pd.read_parquet(MANUAL_FILE) if MANUAL_FILE.exists() else None


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

    With prices/priced_at_index_coupon.parquet (cds_pricer.py) every conv_spread and its upfront / all_upfront / risky_pv01 are shown AT THE
    INDEX COUPON: states 'ok' (quoted at the index coupon), 'recoupon' (quoted at the other coupon, re-priced), 'recal' (a wide name whose
    conv_spread only solves at a marked-down recovery; recovery_x / recovery_y say what was used). Without that file, a Markit conv_spread of
    0 is 'recal' if cds_recalib.py has been run, else 'conv=0'. 'missing' = no Databricks row, 'incomplete' = a row with nothing priced.
    With prices/addin_fill.parquet (addin_fill.py), a cell Databricks could not price ('missing' / 'incomplete') takes the Markit add-in values: state 'addin'.
    A manual override (config/overrides.csv, priced into overrides_priced.parquet) beats all of these (state 'override').
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
            h.loc[fix, 'recovery_used'] = h.loc[fix, 'rec_used']        # the recovery behind every priced number (40% unless a wide name was marked down)
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
            cols = ['conv_spread', 'upfront', 'all_upfront', 'risky_pv01', 'recovery_used']
            h = h.merge(ad[ADDIN_KEY + cols].rename(columns={c: c + '_ad' for c in cols}), on=ADDIN_KEY, how='left')
            gap = h['state'].isin(['missing', 'incomplete']) & h['conv_spread_ad'].notna()
            h.loc[gap, 'value'] = h.loc[gap, 'conv_spread_ad']
            for c in ('upfront', 'all_upfront', 'risky_pv01'):
                h.loc[gap, c] = h.loc[gap, c + '_ad']
            h.loc[gap, 'running_coupon'] = h.loc[gap, 'index_coupon']      # the add-in is queried at the index coupon
            h.loc[gap, 'recovery_used'] = h.loc[gap, 'recovery_used_ad']
            h.loc[gap, 'state'] = 'addin'
        mo = _load_manual()
        if mo is not None:        # a manual override beats every other source for the cells it lists (it can also correct a wrong Databricks value)
            cols = ['conv_spread', 'upfront', 'all_upfront', 'risky_pv01', 'recovery_used']
            h = h.merge(mo[ADDIN_KEY + cols].rename(columns={c: c + '_mo' for c in cols}), on=ADDIN_KEY, how='left')
            man = h['conv_spread_mo'].notna()
            h.loc[man, 'value'] = h.loc[man, 'conv_spread_mo']
            for c in ('upfront', 'all_upfront', 'risky_pv01'):
                h.loc[man, c] = h.loc[man, c + '_mo']
            h.loc[man, 'running_coupon'] = h.loc[man, 'index_coupon']
            h.loc[man, 'recovery_used'] = h.loc[man, 'recovery_used_mo']
            h.loc[man, 'state'] = 'override'
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
    bad = {'missing', 'incomplete', 'conv=0', 'recal', 'recoupon', 'addin', 'override'}
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
WHAT_MANUAL = 'Priced from a manual override (ballpark conv_spread)'
_WHAT_ORDER = [WHAT_NEVER, WHAT_CONV0, WHAT_INCOMPLETE, WHAT_GAP, WHAT_RECAL, WHAT_RECOUPON, WHAT_ADDIN, WHAT_MANUAL]
_BAD_PATTERN = 'missing|incomplete|conv=0'      # a status containing one of these means no price on that date; 'recal' / 'recoupon' alone are priced


def priced_counts(res):
    """(priced at the index coupon, re-priced from the other coupon, wide name via marked-down recovery, priced from the Markit add-in, manual override, not priced)
    member counts. A name counts as not priced if it is on either date; otherwise the most derived source on either date wins."""
    st = res['status']
    unpriced = st.str.contains(_BAD_PATTERN)
    manual = st.str.contains('override') & ~unpriced
    addin = st.str.contains('addin') & ~unpriced & ~manual
    recal = st.str.contains('recal') & ~unpriced & ~manual & ~addin
    recoupon = st.str.contains('recoupon') & ~unpriced & ~manual & ~addin & ~recal
    own = ~unpriced & ~manual & ~addin & ~recal & ~recoupon
    return tuple(int(x.sum()) for x in (own, recoupon, recal, addin, manual, unpriced))


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
    mo = _load_manual()
    if mo is not None:
        real = pd.concat([real, mo.loc[mo['index_name'] == idx, ['market_cds_ticker', 'published_date']]], ignore_index=True)
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
    flagged.loc[~st.str.contains(_BAD_PATTERN) & st.str.contains('override'), 'what'] = WHAT_MANUAL
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
    manual = flagged[flagged['what'] == WHAT_MANUAL]
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
    if not manual.empty:
        names = ', '.join(_html.escape(f"{r.member_name} ({r.Market_CDS_Ticker})") for r in manual.itertuples())
        parts.append(f'<div style="border:2px solid #00897b;background:#e0f2f1;color:#0b0b0b;padding:10px 14px;border-radius:6px;margin-top:8px;line-height:1.6">'
                     f'<b>&#9998; {len(manual)} names use a manual override</b> '
                     f'(a hand-entered ballpark conv_spread, with upfront / all_upfront / risky_pv01 derived by Quantifi; an estimate, not a market quote): {names}</div>')
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
    st['manual'] = False
    ad = _load_addin()
    if ad is not None:
        st = st.drop(columns='addin').merge(ad[ADDIN_KEY].assign(addin=True), on=ADDIN_KEY, how='left')
        st['addin'] = st['addin'].fillna(False).astype(bool) & ~st['priced']
    mo = _load_manual()
    if mo is not None:
        st = st.drop(columns='manual').merge(mo[ADDIN_KEY].assign(manual=True), on=ADDIN_KEY, how='left')
        st['manual'] = st['manual'].fillna(False).astype(bool) & ~st['priced'] & ~st['addin']
    g = st.groupby('index_name').agg(cells=('priced', 'size'), priced=('priced', 'sum'), own_coupon=('own_coupon', 'sum'), addin=('addin', 'sum'), manual=('manual', 'sum'),
                                    no_row=('no_row', 'sum'), empty_row=('empty_row', 'sum'), conv0=('conv0', 'sum'))
    g = g.loc[[i for i in load_index_list()['index_name'] if i in g.index]]
    g.loc['TOTAL'] = g.sum()
    out = pd.DataFrame({'cells': g['cells'], 'priced': g['priced'], 'priced %': g['priced'] / g['cells'] * 100,
                        'quoted at index coupon %': g['own_coupon'] / g['cells'] * 100, 'not priced': g['cells'] - g['priced'],
                        'filled from add-in': g['addin'], 'manual overrides': g['manual'], 'priced % incl. fills': (g['priced'] + g['addin'] + g['manual']) / g['cells'] * 100,
                        'still not priced': g['cells'] - g['priced'] - g['addin'] - g['manual'], 'no row': g['no_row'], 'empty row': g['empty_row'],
                        'conv=0 (priced via par)': g['conv0']})
    ints = ('cells', 'priced', 'not priced', 'filled from add-in', 'manual overrides', 'still not priced', 'no row', 'empty row', 'conv=0 (priced via par)')
    return out.astype({c: int for c in ints}).rename_axis('index').reset_index()


def show_table(df, numeric=(), flag_col=None, decimals=1, align='center'):
    """HTML table for notebooks: nothing wraps, `numeric` columns always show a fixed number of decimals (`decimals`, or a
    {column: decimals} dict), missing values are blank, dates show as YYYY-MM-DD. Rows where `flag_col` is not 'ok' are shaded:
    red and bold if it says missing / incomplete / conv=0, amber and bold if it says recal, teal if it says override, purple if it says addin, blue if it only says recoupon.
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
        elif 'override' in status:
            tr = ' style="background:#e0f2f1;color:#0b0b0b"'
        elif 'addin' in status:
            tr = ' style="background:#f3edfa;color:#0b0b0b"'
        else:
            tr = ' style="background:#eaf2fb;color:#0b0b0b"'
        tds = ''.join(f'<td style="text-align:{al(c)};white-space:nowrap;padding:2px 10px;color:{ink}">{cell(c, rec[c])}</td>'
                      for c in df.columns)
        rows.append(f'<tr{tr}>{tds}</tr>')
    return HTML(f'<table style="border-collapse:collapse;font-size:90%;color:#ffffff;background:#000000"><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table>')
