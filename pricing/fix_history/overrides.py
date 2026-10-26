"""Turn the owner's ballpark conv_spreads (config/overrides.csv) into fully priced cells (cache/overrides_priced.parquet). Needs the SYSTEM Python (Quantifi Toolkit).

    python pricing/fix_history/overrides.py             # price every row of config/overrides.csv
    python pricing/fix_history/overrides.py --dry-run   # expand and check only, nothing is written

config/overrides.csv (one row per fix; paste rows from cache/review/gaps.csv, whose first columns have this layout):
    index_name         e.g. HY41; blank = every index the name is in
    market_cds_ticker  e.g. SIRI
    start_date         first date (YYYY-MM-DD)
    end_date           last date; blank = just start_date
    conv_spread        the conventional spread in bp at the index coupon (HY/XO 500, IG/MA 100) on start_date
    conv_spread_end    optional: the value on end_date; the spread then moves in a straight line between the two (by business day)
    series             optional: a file in config/override_series/ with columns [index_name,] date, conv_spread giving a value PER DAY (e.g. built from Bloomberg by
                       bbg_series.py). Then conv_spread / conv_spread_end can be blank and every cell in the date range takes that day's value (days not in the file are skipped).
    force              blank = only fill cells that have NO price today; 1 = also replace a price that exists (e.g. a wrong Databricks value)
    source, note       free text (where the number came from, e.g. 'bloomberg 5Y PX_LAST'); carried into the output and never used in the maths
Every cell gets par_spread, risky_pv01, upfront, all_upfront by Quantifi (cds_pricer: a flat survival curve through the conv_spread, flat 4% discount, 40% recovery
marked down only for very wide names, risky_pv01 derived). These cells are an estimate, flagged 'override' (teal) in the notebooks. Delete a row and re-run to undo it;
nothing underneath (Databricks, add-in) is ever changed, the overlay just wins where it lists a cell.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'shared'))     # find _paths (it adds the other pricing folders and analysis/)
import _paths  # noqa: F401
import numpy as np
import pandas as pd

import cdx_spreads as cs
import index_metrics as im
from _paths import CONFIG, STORE

SRC = CONFIG / 'overrides.csv'
OUT = STORE / 'prices' / 'overrides_priced.parquet'
COLS = ['index_name', 'market_cds_ticker', 'start_date', 'end_date', 'conv_spread', 'conv_spread_end', 'force', 'source', 'note']
SERIES_DIR = CONFIG / 'override_series'


def _read():
    if not SRC.exists():
        raise FileNotFoundError(f'{SRC} does not exist; create it with the columns {COLS} (see cache/review/gaps.csv)')
    o = pd.read_csv(SRC, dtype={'index_name': str, 'market_cds_ticker': str, 'source': str, 'note': str}, parse_dates=['start_date', 'end_date'], encoding='utf-8-sig')
    missing = [c for c in COLS if c not in o.columns]
    if missing:
        raise ValueError(f'{SRC.name} lacks columns {missing}')
    if 'series' not in o.columns:
        o['series'] = np.nan
    o = o.dropna(subset=['market_cds_ticker', 'start_date']).reset_index(drop=True)
    o = o[o['conv_spread'].notna() | o['series'].notna()].reset_index(drop=True)
    o['end_date'] = o['end_date'].fillna(o['start_date'])
    for c in ('conv_spread', 'conv_spread_end', 'force'):
        o[c] = pd.to_numeric(o[c], errors='coerce')
    bad = o[(o['conv_spread'] <= 0) | (o['end_date'] < o['start_date'])]
    if len(bad):
        raise ValueError(f'rows with a non-positive conv_spread or end_date before start_date:\n{bad}')
    return o


def expand(o=None):
    """One row per overridden cell (index, ticker, date, maturity) with its ballpark conv_spread, plus whether it replaces an existing price."""
    o = _read() if o is None else o
    all_idx = list(cs.load_index_list()['index_name'])
    cells = []
    el_cache = {}
    for r in o.itertuples():
        for idx in ([r.index_name] if isinstance(r.index_name, str) and r.index_name else all_idx):
            if idx not in el_cache:
                el_cache[idx] = im.name_el(idx, manual=False)           # the picture WITHOUT overrides
            el = el_cache[idx]
            g = el[(el['market_cds_ticker'] == r.market_cds_ticker) & (el['published_date'] >= r.start_date) & (el['published_date'] <= r.end_date)].copy()
            if g.empty:
                if isinstance(r.index_name, str) and r.index_name:
                    print(f'warning: {idx} {r.market_cds_ticker} {r.start_date:%Y-%m-%d}..{r.end_date:%Y-%m-%d} matches no cell (name not in that index on those dates)')
                continue
            g = g.sort_values('published_date')
            g['has_price'] = g['all_upfront'].notna()
            if r.force != 1:
                g = g[~g['has_price']]
            if g.empty:
                continue
            if isinstance(r.series, str) and r.series:
                ser = pd.read_csv(SERIES_DIR / r.series, parse_dates=['date'])
                if 'index_name' in ser.columns:
                    ser = ser[ser['index_name'] == idx]
                g = g.merge(ser[['date', 'conv_spread']].rename(columns={'date': 'published_date'}), on='published_date', how='inner')
                if g.empty:
                    print(f'warning: line {r.Index + 2}: series {r.series} has no value for {idx} {r.market_cds_ticker} in the date range')
                    continue
                if (g['conv_spread'] <= 0).any():
                    raise ValueError(f'series {r.series} has non-positive conv_spreads')
            else:
                n = len(g)
                end = r.conv_spread_end if pd.notna(r.conv_spread_end) else r.conv_spread
                g['conv_spread'] = r.conv_spread if n == 1 else np.linspace(r.conv_spread, end, n)
            g['index_name'] = idx
            g['override_source'] = r.source if isinstance(r.source, str) else ''
            g['override_note'] = r.note if isinstance(r.note, str) else ''
            g['row'] = r.Index + 2                                         # line number in overrides.csv (header = line 1)
            cells.append(g[['index_name', 'market_cds_ticker', 'member_name', 'published_date', 'cds_maturity', 'conv_spread', 'has_price', 'defaulted',
                            'override_source', 'override_note', 'row']])
    if not cells:
        return pd.DataFrame(columns=['index_name', 'market_cds_ticker', 'member_name', 'published_date', 'cds_maturity', 'conv_spread', 'has_price', 'defaulted',
                                     'override_source', 'override_note', 'row'])
    out = pd.concat(cells, ignore_index=True)
    dup = out.duplicated(['index_name', 'market_cds_ticker', 'published_date'], keep=False)
    if dup.any():
        raise ValueError(f'overlapping override rows (lines {sorted(out.loc[dup, "row"].unique())} of overrides.csv) cover the same cell(s):\n'
                         f'{out[dup].sort_values(["index_name", "market_cds_ticker", "published_date"]).head(10)}')
    return out


def price_overrides(dry_run=False):
    cells = expand()
    print(f'{len(cells):,} cells from {cells["row"].nunique() if len(cells) else 0} override rows; {int(cells["has_price"].sum()) if len(cells) else 0:,} replace an existing price (force=1)')
    if dry_run or cells.empty:
        if cells.empty and not dry_run:
            OUT.unlink(missing_ok=True)        # no overrides left: remove the overlay so the old ones stop applying
        return cells
    import cds_pricer as cp                    # needs the Quantifi Toolkit (system Python)
    rows = cells.assign(running_coupon=cells['index_name'].map(cs.index_coupon).astype(float),
                        currency=np.where(cells['index_name'].str[:2].isin(['XO', 'MA']), 'EUR', 'USD'))
    priced = cp.price_rows(rows[['published_date', 'cds_maturity', 'conv_spread', 'running_coupon', 'currency']], rows['running_coupon'])
    out = cells.assign(index_coupon=rows['running_coupon'].astype(int))
    for c in ('par_spread', 'risky_pv01', 'upfront', 'all_upfront', 'conv_status', 'recovery_used', 'route'):
        out[c] = priced[c]
    fail = out['all_upfront'].isna()
    if fail.any():
        print(f'WARNING: {int(fail.sum())} cells could not be priced (not written): {out.loc[fail, ["index_name", "market_cds_ticker", "published_date", "conv_spread", "route"]].head(10).to_string()}')
    out = out[~fail].assign(source='manual').drop(columns=['has_price', 'row'])
    out.to_parquet(OUT, index=False)
    print(f'wrote {len(out):,} priced override cells to {OUT.name}; tickers: {", ".join(sorted(out["market_cds_ticker"].unique()))}')
    return out


if __name__ == '__main__':
    price_overrides(dry_run='--dry-run' in sys.argv)
