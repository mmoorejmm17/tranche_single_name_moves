"""Index-level metrics built from the single-name all_upfront prices (one row per index and date).

all_upfront is each name's present value of expected loss, in points (coupon-free: all_upfront = par_spread x risky_pv01 / 100). For one index and one date:

    dispersion        standard deviation of all_upfront across the index's priced names (unweighted; population std, ddof=0, divide by N; set DDOF = 1 for the sample version)
    index_el          index expected loss = sum over priced names of weight x all_upfront / total weight of the names live on that date (so full coverage gives the weighted average)
    topN_share        the N widest names (largest all_upfront): sum(weight x all_upfront) over them / sum over all priced names, e.g. top5_share, top10_share
Weights are fractions of the ORIGINAL index notional (1/125 IG and MA, 1/100 HY, 1/75 XO). Names without a price on a date are excluded from that date (n_priced and priced_weight
say how much of the index that is). all_upfront comes from Databricks (Markit's stored value) where the name is priced there, else from the Markit add-in fill.
Needs no Quantifi Toolkit, so it runs in the project .venv.

DEFAULTED NAMES. Bloomberg's roster drops a defaulted name (weight 0), so the store never held it. teams.structured_credit_abhutra.credit_events says when each one left a series
(index_ex_date). Such a name stays in its series at its original weight, with its own Databricks prices, up to the day before index_ex_date, and drops out from then on. The owner caches the
events and those prices with pricing/fix_history/defaulted.py (needs a Databricks login); without that cache the metrics fall back to the live roster only.
"""
import numpy as np
import pandas as pd

import cdx_spreads as cs

TOP_N = (5, 10)
DDOF = 0          # population standard deviation (divide by N); 1 = sample (N - 1, Excel STDEV.S)
METRICS_FILE = cs.METRICS_DIR / 'index_metrics.parquet'
EVENTS_FILE = cs.REF_DIR / 'credit_events.parquet'
DEFAULTED_FILE = cs.REF_DIR / 'defaulted_prices.parquet'
DEFAULTED_FILL_FILE = cs.PRICES_DIR / 'addin_fill_defaulted.parquet'


# ---- defaulted names (refresh_defaulted, which WRITES the cache, lives in pricing/fix_history/defaulted.py) --------------------------------------
def _defaulted(index_name):
    """Defaulted members of a series: DataFrame(market_cds_ticker, ex_date) and their cached prices, or empty frames if refresh_defaulted() has not been run."""
    if not (EVENTS_FILE.exists() and DEFAULTED_FILE.exists()):
        return pd.DataFrame(columns=['market_cds_ticker', 'ex_date']), pd.DataFrame(columns=['market_cds_ticker', 'published_date', 'cds_maturity', 'all_upfront'])
    ev = pd.read_parquet(EVENTS_FILE)
    ev = ev[ev['index_name'] == index_name][['sn_ticker', 'index_ex_date']].rename(columns={'sn_ticker': 'market_cds_ticker', 'index_ex_date': 'ex_date'}).drop_duplicates()
    px = pd.read_parquet(DEFAULTED_FILE)
    px = px[px['index_name'] == index_name].drop(columns=['index_name', 'ex_date']).assign(source='databricks')
    if DEFAULTED_FILL_FILE.exists():           # days Databricks has no price for, filled from the Markit add-in (addin_fill.py assemble-defaulted)
        a = pd.read_parquet(DEFAULTED_FILL_FILE)
        a = a[a['index_name'] == index_name][['market_cds_ticker', 'published_date', 'cds_maturity', 'all_upfront']].assign(source='addin')
        px = pd.concat([px, a], ignore_index=True).drop_duplicates(['market_cds_ticker', 'published_date'])
    return ev.reset_index(drop=True), px


# ---- per-name expected loss -------------------------------------------------------------------------------------------------------
def name_el(index_name, manual=True):
    """One row per (member, date) with that name's all_upfront (expected loss, points) and its weight as a fraction of the original index notional; NaN where unpriced.
    Defaulted names appear (from their Databricks prices) up to the day before their index_ex_date."""
    members = cs.get_constituents(index_name)
    hist = cs.get_spread_history(index_name)
    el = hist[['member_name', 'market_cds_ticker', 'published_date', 'cds_maturity', 'all_upfront']].copy()
    el['source'] = np.where(el['all_upfront'].notna(), 'databricks', '')
    ad = cs._load_addin()
    if ad is not None:
        a = ad.loc[ad['index_name'] == index_name, ['market_cds_ticker', 'published_date', 'cds_maturity', 'all_upfront']].rename(columns={'all_upfront': 'el_ad'})
        el = el.merge(a, on=['market_cds_ticker', 'published_date', 'cds_maturity'], how='left')
        use = el['all_upfront'].isna() & el['el_ad'].notna()
        el.loc[use, 'all_upfront'] = el.loc[use, 'el_ad']
        el.loc[use, 'source'] = 'addin'
        el = el.drop(columns='el_ad')
    w = members.drop_duplicates('member_name').set_index('member_name')['weight'] / 100.0
    el['weight'] = el['member_name'].map(w)
    el['defaulted'] = False
    ev, px = _defaulted(index_name)
    if len(ev):
        std_w = float(w.mode().iloc[0])                                    # equal-weighted index: a defaulted name carried the same weight as every other member
        days = hist[['published_date']].drop_duplicates()
        d = ev.merge(days, how='cross')
        d = d[d['published_date'] < d['ex_date']]                           # every day it was still in the series; days without a price stay NaN (they show up as gaps)
        d = d.merge(px[['market_cds_ticker', 'published_date', 'all_upfront', 'source']], on=['market_cds_ticker', 'published_date'], how='left')
        d['source'] = d['source'].fillna('')
        d = d.assign(member_name=d['market_cds_ticker'] + ' (defaulted)', cds_maturity=cs.get_index_maturity(index_name), weight=std_w, defaulted=True).drop(columns='ex_date')
        el = pd.concat([el, d[el.columns]], ignore_index=True)
    mo = cs._load_manual() if manual else None      # manual=False: the picture WITHOUT the overrides (pricing/fix_history uses it to see what is still a gap)
    if mo is not None:         # a manual override beats every other source for the cells it lists
        m = mo.loc[mo['index_name'] == index_name, ['market_cds_ticker', 'published_date', 'cds_maturity', 'all_upfront']].rename(columns={'all_upfront': 'el_mo'})
        el = el.merge(m, on=['market_cds_ticker', 'published_date', 'cds_maturity'], how='left')
        use = el['el_mo'].notna()
        el.loc[use, 'all_upfront'] = el.loc[use, 'el_mo']
        el.loc[use, 'source'] = 'manual'
        el = el.drop(columns='el_mo')
    return el


def _live(index_name, dates, std_w, n_roster, roster_w):
    """Per date: number of live members and their total weight (the roster plus the defaulted names whose index_ex_date is still ahead)."""
    ev, _ = _defaulted(index_name)
    n = pd.Series(n_roster, index=dates, dtype=float)
    wt = pd.Series(roster_w, index=dates, dtype=float)
    for r in ev.itertuples():
        before = dates < r.ex_date
        n[before] += 1
        wt[before] += std_w
    return n, wt


def index_metrics(index_name, top_ns=TOP_N, ddof=DDOF, el=None):
    """Per-date metrics for one index: dispersion, index_el, topN_share, n_members (live names), n_priced, priced_weight (share of the live weight priced), max_el."""
    el = name_el(index_name) if el is None else el
    dates = pd.DatetimeIndex(sorted(cs.get_spread_history(index_name)['published_date'].unique()), name='published_date')
    ros = el[~el['defaulted']].drop_duplicates('member_name')
    n_live, w_live = _live(index_name, dates, float(ros['weight'].mode().iloc[0]), ros['member_name'].nunique(), float(ros['weight'].sum()))
    d = el[el['all_upfront'].notna()].copy()
    d['wel'] = d['weight'] * d['all_upfront']
    d['rank'] = d.groupby('published_date')['all_upfront'].rank(method='first', ascending=False)
    g = d.groupby('published_date')
    out = pd.DataFrame({'dispersion': g['all_upfront'].std(ddof=ddof), 'n_priced': g['all_upfront'].size(), 'max_el': g['all_upfront'].max(), 'wel_sum': g['wel'].sum(),
                        'w_priced': g['weight'].sum()}).reindex(dates)
    out['n_members'] = n_live
    out['index_el'] = out['wel_sum'] / w_live
    out['priced_weight'] = out['w_priced'] / w_live
    for n in top_ns:
        out[f'top{n}_share'] = d['wel'].where(d['rank'] <= n).groupby(d['published_date']).sum().reindex(dates) / out['wel_sum']
    out['n_priced'] = out['n_priced'].fillna(0).astype(int)
    out['n_members'] = out['n_members'].astype(int)
    out.insert(0, 'index_name', index_name)
    cols = ['index_name', 'n_members', 'dispersion', 'index_el', 'n_priced', 'priced_weight', 'max_el'] + [f'top{n}_share' for n in top_ns]
    return out[cols].reset_index()


def all_index_metrics(index_names=None, out_file=None):
    """index_metrics for every index (or the given ones), stacked (exactly one row per index and date). Writes parquet only if out_file is given
    (pricing/shared/publish.py does that; the analysis side never writes into data/)."""
    names = list(index_names) if index_names else list(cs.load_index_list()['index_name'])
    out = pd.concat([index_metrics(n) for n in names], ignore_index=True)
    assert not out.duplicated(['index_name', 'published_date']).any()
    if out_file is not None:
        out.to_parquet(out_file, index=False)
    return out


def widest(index_name, date, n=10):
    """The n widest names on a date with their all_upfront, weight, expected-loss contribution and share of the total (for inspecting a topN_share)."""
    el = name_el(index_name)
    d = el[(el['published_date'] == pd.Timestamp(date)) & el['all_upfront'].notna()].copy()
    d['el_contribution'] = d['weight'] * d['all_upfront']
    d['share_of_index_el'] = d['el_contribution'] / d['el_contribution'].sum()
    return d.sort_values('all_upfront', ascending=False).head(n)[['member_name', 'market_cds_ticker', 'all_upfront', 'weight', 'el_contribution', 'share_of_index_el', 'source']]


# ---- diagnostics used by dispersion.ipynb and topN.ipynb --------------------------------------------------------------------------
def name_coverage(index_name, el=None):
    """One row per member (live roster plus defaulted names): how often it has a price on the days it was in the index.

    Columns: member_name, ticker, defaulted, live_days, priced_days, priced_pct, first_priced, last_priced, longest_gap (longest run of consecutive unpriced live dates),
    addin_days (priced days that came from the Markit add-in). Sparse names have a low priced_pct or a long longest_gap."""
    el = name_el(index_name) if el is None else el
    dates = pd.DatetimeIndex(sorted(cs.get_spread_history(index_name)['published_date'].unique()))
    ev, _ = _defaulted(index_name)
    ex = dict(zip(ev['market_cds_ticker'], ev['ex_date']))
    rows = []
    for (member, ticker, dflt), g in el.groupby(['member_name', 'market_cds_ticker', 'defaulted'], dropna=False):
        live = dates[dates < ex[ticker]] if dflt and ticker in ex else dates
        pr = g[g['all_upfront'].notna()]
        priced = pd.DatetimeIndex(pr['published_date'].unique())
        flag = ~live.isin(priced)
        longest = cur = 0
        for f in flag:
            cur = cur + 1 if f else 0
            longest = max(longest, cur)
        rows.append(dict(member_name=member, ticker=ticker, defaulted=bool(dflt), live_days=len(live), priced_days=int((~flag).sum()),
                         priced_pct=100.0 * (~flag).sum() / max(len(live), 1), first_priced=priced.min() if len(priced) else pd.NaT,
                         last_priced=priced.max() if len(priced) else pd.NaT, longest_gap=longest, addin_days=int((pr['source'] == 'addin').sum())))
    return pd.DataFrame(rows).sort_values(['priced_pct', 'longest_gap'], ascending=[True, False]).reset_index(drop=True)


def missing_on(index_name, date, el=None):
    """Names that were in the index on `date` but have no price that day (live roster names with no all_upfront, plus defaulted names not yet at their ex-date)."""
    date = pd.Timestamp(date)
    el = name_el(index_name) if el is None else el
    d = el[el['published_date'] == date]
    miss = list(d.loc[d['all_upfront'].isna(), 'member_name'])
    ev, _ = _defaulted(index_name)
    have = set(d.loc[d['all_upfront'].notna(), 'market_cds_ticker'])
    miss += [f'{r.market_cds_ticker} (defaulted)' for r in ev.itertuples() if date < r.ex_date and r.market_cds_ticker not in have]
    return miss


def ranked(index_name, date, el=None):
    """Priced names on a date ranked by all_upfront (rank 1 = widest) with weight, expected-loss contribution and share of the total contribution."""
    el = name_el(index_name) if el is None else el
    d = el[(el['published_date'] == pd.Timestamp(date)) & el['all_upfront'].notna()].copy()
    d['el_contribution'] = d['weight'] * d['all_upfront']
    d['share'] = d['el_contribution'] / d['el_contribution'].sum()
    d = d.sort_values('all_upfront', ascending=False).reset_index(drop=True)
    d['rank'] = np.arange(1, len(d) + 1)
    return d[['rank', 'member_name', 'market_cds_ticker', 'all_upfront', 'weight', 'el_contribution', 'share', 'source']]
