"""Cache the credit events and the defaulted names' prices into the working copy cache/ (needs a Databricks login). Owner only; publish.py ships the result."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared"))     # find _paths (it adds the other pricing folders and analysis/)
import _paths  # noqa: F401
import pandas as pd

import cdx_spreads as cs
import cdx_store as st
import index_metrics as im

EVENTS_TABLE = 'teams.structured_credit_abhutra.credit_events'


def refresh_defaulted():
    """Cache the credit events (for the 40 series we track) and each defaulted name's Databricks all_upfront at its series' maturity, from the series launch to the day
    before index_ex_date. Writes cache/credit_events.parquet and cache/defaulted_prices.parquet. Needs a Databricks login (browser window the first time)."""
    conn = st.get_conn()
    try:
        ev = pd.read_sql(f'SELECT sn_ticker, index_name, auction_date, index_ex_date, index_loss, index_recovery FROM {EVENTS_TABLE}', conn)
        ev['auction_date'] = pd.to_datetime(ev['auction_date'])
        ev['index_ex_date'] = pd.to_datetime(ev['index_ex_date'])
        ev = ev[ev['index_name'].isin(cs.load_index_list()['index_name'])].reset_index(drop=True)
        ev['maturity'] = [cs.get_index_maturity(i) for i in ev['index_name']]
        ev['launch'] = [st.index_launch_date(i) for i in ev['index_name']]
        tick = ', '.join(f"'{t}'" for t in sorted(ev['sn_ticker'].unique()))
        mats = ', '.join(f"'{m:%Y-%m-%d}'" for m in sorted(ev['maturity'].unique()))
        px = pd.read_sql(f'SELECT market_cds_ticker, published_date, cds_maturity, running_coupon, all_upfront, par_spread, risky_pv01 FROM {st.TABLE} '
                         f"WHERE market_cds_ticker IN ({tick}) AND cds_maturity IN ({mats}) AND published_date >= '{st.HISTORY_START}' AND all_upfront IS NOT NULL", conn)
    finally:
        conn.close()
    px['published_date'] = pd.to_datetime(px['published_date'])
    px['cds_maturity'] = pd.to_datetime(px['cds_maturity'])
    ev.to_parquet(im.EVENTS_FILE, index=False)
    rows = []
    for e in ev.itertuples():
        p = px[(px['market_cds_ticker'] == e.sn_ticker) & (px['cds_maturity'] == e.maturity) & (px['published_date'] >= e.launch) & (px['published_date'] < e.index_ex_date)]
        p = p.drop_duplicates(['published_date'])
        rows.append(p.assign(index_name=e.index_name, ex_date=e.index_ex_date)[['index_name', 'market_cds_ticker', 'published_date', 'cds_maturity', 'all_upfront', 'ex_date']])
    out = pd.concat(rows, ignore_index=True)
    out.to_parquet(im.DEFAULTED_FILE, index=False)
    exp = pd.DataFrame({'index_name': ev['index_name'], 'ticker': ev['sn_ticker'], 'ex': ev['index_ex_date'], 'launch': ev['launch']})
    got = out.groupby(['index_name', 'market_cds_ticker']).size().rename('priced_days')
    print(f'{len(ev)} credit events in our series; {len(out):,} priced name-days cached for {out.groupby(["index_name", "market_cds_ticker"]).ngroups} of {len(ev)} (name, series) pairs')
    return ev, out


if __name__ == '__main__':
    refresh_defaulted()
