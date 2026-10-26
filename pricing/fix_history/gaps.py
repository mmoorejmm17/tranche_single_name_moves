"""List every place the history still has no price, as runs of consecutive dates per (index, name), with the neighbouring prices to ballpark from.

    python pricing/fix_history/gaps.py              # writes cache/review/gaps.csv (open it in Excel) and prints a summary

Reads the working copy (cache/). Columns are laid out so index_name .. note can be copied straight into config/overrides.csv:
    index_name, market_cds_ticker, start_date, end_date, conv_spread, conv_spread_end, force, source, note   (the five to fill in are blank here)
then info columns: member_name, defaulted, n_days, kind, prev_date / prev_conv, next_date / next_conv, suggested_conv.
`kind`: interior (priced before and after: interpolate), leading (priced only after), trailing (priced only before), never (no price anywhere in this index).
`suggested_conv` is a straight line between the neighbouring conv_spreads (flat if only one side), only a starting point. Defaulted names have no conv_spread
history here (their prices are all_upfront), so they show no neighbours.
Cells already covered by an override are NOT listed (they are no longer gaps); `--include-overrides` lists them too.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'shared'))     # find _paths (it adds the other pricing folders and analysis/)
import _paths  # noqa: F401
import numpy as np
import pandas as pd

import cdx_spreads as cs
import index_metrics as im
from _paths import CACHE

OUT = CACHE / 'review' / 'gaps.csv'
OVERRIDE_COLS = ['index_name', 'market_cds_ticker', 'start_date', 'end_date', 'conv_spread', 'conv_spread_end', 'force', 'source', 'note']


def _conv_series(index_name):
    """conv_spread at the index coupon per (ticker, date) for this index, in waterfall order: manual, Databricks (re-priced), add-in."""
    mat, ic = cs.get_index_maturity(index_name), cs.index_coupon(index_name)
    parts = []
    mo = cs._load_manual()
    if mo is not None:
        parts.append(mo.loc[mo['index_name'] == index_name, ['market_cds_ticker', 'published_date', 'conv_spread']])
    pr = cs._load_priced()
    if pr is not None:
        pr = pr[(pr['cds_maturity'] == mat) & (pr['index_coupon'] == ic)]
        parts.append(pr[['market_cds_ticker', 'published_date', 'conv_spread']])
    ad = cs._load_addin()
    if ad is not None:
        parts.append(ad.loc[ad['index_name'] == index_name, ['market_cds_ticker', 'published_date', 'conv_spread']])
    s = pd.concat(parts, ignore_index=True).dropna(subset=['conv_spread']).drop_duplicates(['market_cds_ticker', 'published_date'])
    return s


def _gaps_for(index_name, include_overrides):
    el = im.name_el(index_name, manual=not include_overrides)
    miss = el[el['all_upfront'].isna()].copy()
    if miss.empty:
        return []
    dates = pd.DatetimeIndex(sorted(cs.get_spread_history(index_name)['published_date'].unique()))
    pos = pd.Series(np.arange(len(dates)), index=dates)
    conv = _conv_series(index_name)
    miss['pos'] = miss['published_date'].map(pos)
    out = []
    for (tk, name, dflt), g in miss.groupby(['market_cds_ticker', 'member_name', 'defaulted']):
        g = g.sort_values('pos')
        run = (g['pos'].diff() != 1).cumsum()
        c = conv[conv['market_cds_ticker'] == tk].sort_values('published_date')
        for _, r in g.groupby(run):
            a, b = r['published_date'].min(), r['published_date'].max()
            before = c[c['published_date'] < a].tail(1)
            after = c[c['published_date'] > b].head(1)
            pc = float(before['conv_spread'].iloc[0]) if len(before) else np.nan
            nc = float(after['conv_spread'].iloc[0]) if len(after) else np.nan
            kind = 'interior' if len(before) and len(after) else 'leading' if len(after) else 'trailing' if len(before) else 'never'
            if dflt:
                kind = 'defaulted (all_upfront only)'
            out.append(dict(index_name=index_name, market_cds_ticker=tk, start_date=a, end_date=b, conv_spread='', conv_spread_end='', force='', source='', note='',
                            member_name=name, defaulted=bool(dflt), n_days=len(r), kind=kind,
                            prev_date=before['published_date'].iloc[0] if len(before) else pd.NaT, prev_conv=pc,
                            next_date=after['published_date'].iloc[0] if len(after) else pd.NaT, next_conv=nc))
    return out


def find_gaps(index_names=None, include_overrides=False):
    """DataFrame of gap runs (see module doc). include_overrides=True lists cells that an override currently fills as well."""
    names = list(index_names) if index_names else list(cs.load_index_list()['index_name'])
    rows = []
    for n in names:
        rows += _gaps_for(n, include_overrides)
    g = pd.DataFrame(rows, columns=OVERRIDE_COLS + ['member_name', 'defaulted', 'n_days', 'kind', 'prev_date', 'prev_conv', 'next_date', 'next_conv'])
    if g.empty:
        return g.assign(suggested_conv=[])
    both = g['prev_conv'].notna() & g['next_conv'].notna()
    mid = (g['start_date'] + (g['end_date'] - g['start_date']) / 2)
    span = (g['next_date'] - g['prev_date']).dt.days.where(both)
    frac = ((mid - g['prev_date']).dt.days / span)
    g['suggested_conv'] = np.where(both, g['prev_conv'] + (g['next_conv'] - g['prev_conv']) * frac, g['prev_conv'].fillna(g['next_conv'])).round(1)
    return g.sort_values(['kind', 'market_cds_ticker', 'index_name', 'start_date']).reset_index(drop=True)


def summary(g):
    if g.empty:
        print('no gaps: every name is priced on every date')
        return
    cells = g['n_days'].sum()
    print(f'{cells:,} unpriced cells in {len(g):,} runs, {g["market_cds_ticker"].nunique()} names across {g["index_name"].nunique()} indices')
    print(g.groupby('kind')['n_days'].agg(runs='size', cells='sum').to_string())
    top = g.groupby('market_cds_ticker')['n_days'].sum().sort_values(ascending=False).head(15)
    print('largest by cells:', ', '.join(f'{t} {n:,}' for t, n in top.items()))


if __name__ == '__main__':
    gaps = find_gaps(include_overrides='--include-overrides' in sys.argv)
    OUT.parent.mkdir(exist_ok=True)
    gaps.to_csv(OUT, index=False, date_format='%Y-%m-%d', encoding='utf-8-sig')
    summary(gaps)
    print(f'wrote {OUT}')
