"""Best-guess conv_spread for the dates a name has no price (used by review.py to fill the review sheet).

Method per cell, in order:
  'bbg5y x ratio'  Bloomberg's 5Y CDS for the name, scaled to the index's remaining maturity by the ratio markit_conv(T) / bbg_5y learned from this same name on the dates where
                   both exist (separately for HY/XO 500-coupon and IG/MA 100-coupon indices). Needs a Bloomberg ticker (BBG_5Y_CDS_Ticker in the mappings file) and some overlap.
  'interp'         straight line (by calendar day) between the last priced conv_spread before the run and the first after it.
  'flat'           the nearest priced conv_spread, held, when only one side exists.
  'none'           nothing to go on; the cell is left for the owner.
The result is an ESTIMATE. The in-sample backtest (the same method against Markit's own spreads on the overlap) is returned so the owner can judge it.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'shared'))     # find _paths (it adds the other pricing folders and analysis/)
import _paths  # noqa: F401
import numpy as np
import pandas as pd

import cdx_members
import cdx_spreads as cs
import cdx_store

BIN = 0.25          # years of remaining maturity per ratio bin
MIN_PTS = 5         # points a bin needs to be used


def bbg_ticker_for(ticker):
    m = pd.read_csv(cdx_members.MAPPINGS_PATH, usecols=['CDS_Ticker', 'BBG_5Y_CDS_Ticker'], dtype=str, encoding='cp1252')
    t = m.loc[m['CDS_Ticker'] == ticker, 'BBG_5Y_CDS_Ticker'].dropna()
    return t.iloc[0] if len(t) else None


def bbg_history(bbg_ticker, start, end):
    cdx_store._utils_on_path()
    from bbg_utils import bdh_mm
    d = bdh_mm(bbg_ticker, 'PX_LAST', start, end)
    return d.set_index(pd.to_datetime(d['date']))['PX_LAST'].sort_index()


def markit_conv(ticker):
    """conv_spread at each index's own coupon for every index and date where Markit (Databricks, then add-in) prices this name, with T = years to the index maturity."""
    pr, ad = cs._load_priced(), cs._load_addin()
    out = []
    for i in cs.load_index_list()['index_name']:
        mat, ic = cs.get_index_maturity(i), cs.index_coupon(i)
        a = pr[(pr['market_cds_ticker'] == ticker) & (pr['cds_maturity'] == mat) & (pr['index_coupon'] == ic)][['published_date', 'conv_spread']]
        b = ad[(ad['market_cds_ticker'] == ticker) & (ad['index_name'] == i)][['published_date', 'conv_spread']]
        out.append(pd.concat([a, b]).drop_duplicates('published_date').assign(index_name=i, coupon=ic, T=lambda d, mat=mat: (mat - d['published_date']).dt.days / 365.25))
    return pd.concat(out, ignore_index=True)


def fit_ratio(m, bbg):
    """({coupon: DataFrame(ratio, n, lo, hi) indexed by remaining maturity bin}, the matched points with bbg and ratio)."""
    m = m.assign(bbg=m['published_date'].map(bbg)).dropna(subset=['bbg'])
    m['ratio'] = m['conv_spread'] / m['bbg']
    m['bin'] = (m['T'] / BIN).round() * BIN
    curves = {}
    for cpn, g in m.groupby('coupon'):
        b = g.groupby('bin').agg(ratio=('ratio', 'median'), n=('ratio', 'size'), lo=('ratio', 'min'), hi=('ratio', 'max'))
        curves[cpn] = b[b['n'] >= MIN_PTS]
    return curves, m


def guess_cells(ticker, runs):
    """One row per gap cell for `ticker`: index_name, market_cds_ticker, member_name, date, years_left, bbg_5y, best_guess, guess_method; and an info dict
    (bbg_ticker, curves, backtest = (n, mean abs error, median error, p90 abs error) or None, notes). `runs` is gaps.find_gaps() filtered to this name."""
    info = dict(bbg_ticker=bbg_ticker_for(ticker), curves={}, backtest=None, notes=[])
    bbg, curves = None, {}
    if info['bbg_ticker']:
        try:
            bbg = bbg_history(info['bbg_ticker'], (runs['start_date'].min() - pd.Timedelta(days=10)).strftime('%Y-%m-%d'), pd.Timestamp.today().strftime('%Y-%m-%d'))
            curves, m = fit_ratio(markit_conv(ticker), bbg)
            info['curves'] = curves
            if curves:
                pred = [np.interp(t, curves[c].index.values, curves[c]['ratio'].values) * b if c in curves and len(curves[c]) else np.nan for t, c, b in zip(m['T'], m['coupon'], m['bbg'])]
                err = (pd.Series(pred, index=m.index) - m['conv_spread']).dropna()
                if len(err):
                    info['backtest'] = (len(err), err.abs().mean(), err.median(), err.abs().quantile(.9))
            else:
                info['notes'].append('no overlap between Markit and Bloomberg for this name: bbg x ratio not available')
        except Exception as e:      # Bloomberg off, ticker not found, ...
            info['notes'].append(f'Bloomberg unavailable ({type(e).__name__}: {str(e)[:80]}); using interpolation only')
            bbg = None
    else:
        info['notes'].append('no BBG_5Y_CDS_Ticker in the mappings file; using interpolation only')
    rows = []
    for r in runs.itertuples():
        mat = cs.get_index_maturity(r.index_name)
        cpn = cs.index_coupon(r.index_name)
        dates = pd.DatetimeIndex(sorted(cs.get_spread_history(r.index_name)['published_date'].unique()))
        dates = dates[(dates >= r.start_date) & (dates <= r.end_date)]
        T = np.asarray((mat - dates).days / 365.25)
        px = bbg.reindex(bbg.index.union(dates)).ffill().reindex(dates).values if bbg is not None else np.full(len(dates), np.nan)
        c = curves.get(cpn)
        ratio = np.interp(T, c.index.values, c['ratio'].values) if c is not None and len(c) else np.full(len(dates), np.nan)
        g = px * ratio
        method = np.where(np.isnan(g), '', 'bbg5y x ratio')
        # fallback for the cells bbg x ratio could not give: straight line / flat between the neighbouring Markit prices
        have_p, have_n = pd.notna(r.prev_conv), pd.notna(r.next_conv)
        if have_p or have_n:
            if have_p and have_n:
                frac = np.asarray((dates - r.prev_date).days / max((r.next_date - r.prev_date).days, 1))
                alt, alt_m = r.prev_conv + (r.next_conv - r.prev_conv) * frac, 'interp'
            else:
                alt, alt_m = np.full(len(dates), r.prev_conv if have_p else r.next_conv), 'flat'
            use = np.isnan(g)
            g = np.where(use, alt, g)
            method = np.where(use, alt_m, method)
        method = np.where(np.isnan(g), 'none', method)
        rows.append(pd.DataFrame({'index_name': r.index_name, 'market_cds_ticker': r.market_cds_ticker, 'member_name': r.member_name, 'date': dates, 'years_left': T.round(2),
                                  'bbg_5y': px, 'best_guess': np.round(g, 2), 'guess_method': method}))
    return pd.concat(rows, ignore_index=True), info
