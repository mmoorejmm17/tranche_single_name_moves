"""DAILY workflow: ingest new pricing as it becomes available. Owner only. Use the SYSTEM Python (Bloomberg for rosters, a Databricks login, Quantifi for pricing).

    python pricing/daily/update.py                 # pull new dates for all 40 indices, price only the new points, report what is still unpriced
    python pricing/daily/update.py HY47 IG47       # only some indices
    python pricing/daily/update.py --no-price      # just pull (e.g. no Quantifi available)
    python pricing/daily/update.py --no-addin      # skip the Markit add-in step (e.g. Excel / the Markit login is not available)

THE TWO DAILY STEPS:   1) python pricing/daily/update.py     2) python pricing/shared/publish.py

Steps (all write only the working store cache/base, nothing is published):
  1. cdx_store.update_store: fetch Databricks rows from (latest stored date - 7 days) on, replace that overlap, rebuild each index's members x dates grid. A newest date that
     Databricks has not really loaded yet (under 50% of its cells have a row, e.g. today's date early on) is dropped again, so a half-loaded day never reaches the store.
  2. cds_pricer.price_new: price only the points not yet in prices/priced_at_index_coupon.parquet, plus the last 7 days again (restatements). ~seconds, not the 44 minutes of a full price_store.
  3. The Markit add-in for the names Databricks does not price (opens its own Excel window; needs the Markit login): refresh the tenor labels if the store has newer dates, pull the new
     days for the instruments already cached (addin_fill.pull extends them) and any never pulled, then addin_fill.assemble_new (incremental: ~1 minute, not the ~12 of a full assemble).
     If Excel / the login is not available it says so and carries on; run the add-in pieces later (addin_fill.py pull, assemble-new).
  4. Re-apply the standing overrides in config/overrides.csv (overrides.py; they only fill cells with no price, so a rule such as COMM at a flat 50 bp keeps covering new days).
  5. Report the new dates: cells priced by Databricks, by the add-in, by an override, and every (name, index) still open, with what to do: a review sheet (pricing/fix_history/review.py
     make <TICKER>) for a name that needs a hand estimate.
Then run `python pricing/shared/publish.py` (it keeps the previous published store in data/_previous). This step is separate from pricing/fix_history/, which repairs OLD missing data; see
CLAUDE.md for the planned base / live split that will stop this step touching old dates.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'shared'))     # find _paths (it adds the other pricing folders and analysis/)
import _paths  # noqa: F401
import pandas as pd

import cdx_spreads as cs
import cdx_store


def _store_dates():
    st = pd.read_parquet(cs.STORE_FILE, columns=['published_date'])
    return st['published_date'].max()


MIN_TAIL_PCT = 50.0       # a trailing date with less than this share of its cells holding a Databricks row is treated as not loaded yet


def trim_incomplete_tail(min_pct=MIN_TAIL_PCT):
    """Drop trailing dates that Databricks has only partly (or not at all) loaded, from the working store. update_store builds the grid for every date in the table, so on the day itself
    (or while the day's prices are still being published) the newest date exists with few or no prices; left in, it would show every name as missing. The next update_store re-fetches
    the last 7 days, so the date comes back automatically once it is loaded. Returns the dates dropped."""
    st = pd.read_parquet(cs.STORE_FILE)
    dropped = []
    while True:
        last = st['published_date'].max()
        g = st[st['published_date'] == last]
        pct = g['has_data'].mean() * 100
        if pct >= min_pct:
            break
        dropped.append((last, pct, len(g)))
        st = st[st['published_date'] < last]
    if dropped:
        tmp = cs.STORE_FILE.with_suffix('.tmp')
        st.to_parquet(tmp, index=False)
        tmp.replace(cs.STORE_FILE)
        for d, pct, n in dropped:
            print(f'dropped {d:%Y-%m-%d} from the working store: only {pct:.0f}% of its {n:,} cells have a Databricks row (not loaded yet; it returns on a later update)')
    return [d for d, _, _ in dropped]


def addin_step():
    """Fill what Databricks cannot price from the Markit add-in. Returns True if it ran; a failure (no Excel, no Markit login, no Quantifi) is reported and does not stop the run."""
    try:
        import addin_fill as af
        import markit_addin as ma
        tm = ma.tenor_map()
        if tm['published_date'].max() < _store_dates():
            print('refreshing the tenor labels (the store has newer dates)')
            ma.tenor_map(refresh=True)
        af.pull()
        af.assemble_new()
        return True
    except Exception as e:
        print(f'\nADD-IN STEP DID NOT RUN ({type(e).__name__}: {str(e)[:160]}).\n  Names Databricks does not price will show as open below. When Excel and the Markit login are available run:'
              '\n    python pricing/fix_history/addin_fill.py pull\n    python pricing/fix_history/addin_fill.py assemble-new')
        return False


def overrides_step():
    """Re-apply the standing overrides in config/overrides.csv (e.g. COMM at a flat 50 bp, which has no end date): they fill only cells that have NO price, so each daily run extends them to the new
    days and drops cells the add-in or Databricks has since priced. Needs Quantifi; a failure is reported and does not stop the run."""
    try:
        import overrides as ov
        if ov.SRC.exists():
            ov.price_overrides()
    except Exception as e:
        print(f'\nOVERRIDES STEP DID NOT RUN ({type(e).__name__}: {str(e)[:160]}); run: python pricing/fix_history/overrides.py')


def ingest_new(index_names=None, price=True, addin=True):
    before = _store_dates() if cs.STORE_FILE.exists() else None
    print(f'store runs through {before:%Y-%m-%d}' if before is not None else 'no store yet')
    cdx_store.update_store(index_names)
    trim_incomplete_tail()
    after = _store_dates()
    print(f'store now runs through {after:%Y-%m-%d}' + (f' ({(after - before).days} calendar days newer)' if before is not None else ''))
    if price:
        import cds_pricer as cp                  # needs the Quantifi Toolkit (system Python)
        cp.price_new(index_names)
    if addin:
        addin_step()
    if price:
        overrides_step()
    report(before)


def report(since):
    """The cells on dates after `since` that Databricks could not price: grouped by name with the indices and dates affected."""
    cols = ['index_name', 'member_name', 'market_cds_ticker', 'published_date', 'cds_maturity', 'has_data', 'running_coupon', 'par_spread', 'upfront', 'conv_spread']
    st = pd.read_parquet(cs.STORE_FILE, columns=cols)
    new = st[st['published_date'] > since] if since is not None else st
    if new.empty:
        print('no new dates')
        return
    bad = new[~cs.priced_mask(new)]
    key = ['index_name', 'market_cds_ticker', 'published_date', 'cds_maturity']
    n_addin = n_manual = 0
    ad, mo = cs._load_addin(), cs._load_manual()
    if ad is not None and len(bad):
        hit = bad.merge(ad[key].assign(_a=True), on=key, how='left')['_a'].fillna(False).astype(bool).values
        n_addin, bad = int(hit.sum()), bad[~hit]
    if mo is not None and len(bad):
        hit = bad.merge(mo[key].assign(_m=True), on=key, how='left')['_m'].fillna(False).astype(bool).values
        n_manual, bad = int(hit.sum()), bad[~hit]
    dates = sorted(new['published_date'].unique())
    print(f'\nnew dates: {pd.Timestamp(dates[0]):%Y-%m-%d} to {pd.Timestamp(dates[-1]):%Y-%m-%d} ({len(dates)}); {len(new):,} cells: {len(new) - len(bad) - n_addin - n_manual:,} priced by Databricks, '
          f'{n_addin:,} by the Markit add-in, {n_manual:,} by overrides, {len(bad):,} still open')
    if bad.empty:
        print('every name is priced on the new dates')
        return
    g = (bad.groupby(['market_cds_ticker']).agg(name=('member_name', 'first'), cells=('index_name', 'size'), indices=('index_name', lambda x: ', '.join(sorted(set(x)))),
                                              first=('published_date', 'min'), last=('published_date', 'max')).sort_values('cells', ascending=False))
    print(g.to_string())
    print('\nstill open: a name that stays open has no Databricks or add-in price. If it matters, estimate it with a review sheet (python pricing/fix_history/review.py make <TICKER>).'
          '\nNext: python pricing/shared/publish.py')


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    ingest_new(args or None, price='--no-price' not in sys.argv, addin='--no-addin' not in sys.argv)
