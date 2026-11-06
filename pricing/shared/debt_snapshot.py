"""Snapshot of every CDS name's debt (bonds, loans, RCFs) from Bloomberg BQL, and a diff against the previous snapshot. System Python, needs a running Excel with Bloomberg (see bbg_bql.py).

    python pricing/shared/debt_snapshot.py run [--limit N]     # query the whole mappings universe -> cache/debt/debt_YYYY-MM-DD.parquet (resumable: finished batches are kept)
    python pricing/shared/debt_snapshot.py diff                # newest snapshot vs the one before it (added / removed / changed securities)
One BQL query per UNIQUE equity ticker (own BBG_Equity_Ticker first; several CDS names can share one), then rows are copied to every market_cds_ticker that maps to it. Banks return tens of thousands of notes,
so a full run takes roughly 20-30 min; run it about weekly. Columns: market_cds_ticker, equity_ticker, security_id, security_name, asset_class, currency, issue_date, maturity,
notional_outstanding (USD), notional_local (own currency), notional_total (loans: tranche size; bonds: = outstanding), amt_out_asof (date of the outstanding amount), coupon, payment_rank, security_type (Bloomberg: TERM, REV, LOC, DELAY-DRAW TERM, GLOBAL, ...), pulled_at.
"""
import sys
import time
import hashlib
import pathlib
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import bbg_bql as b

ROOT = pathlib.Path(__file__).resolve().parents[2]
OUT = ROOT / 'cache' / 'debt'
FIELDS = b.FIELDS + ', payment_rank, security_typ'
RENAME = {'id': 'security_id', 'name': 'security_name', 'srch_asset_class': 'asset_class', 'crncy': 'currency', 'issue_dt': 'issue_date',
          'amt_outstanding(currency=USD)': 'notional_outstanding', 'amt_outstanding': 'notional_local', 'ln_tranche_size': 'tranche_size', 'amount_outstanding_as_of_dt': 'amt_out_asof', 'cpn': 'coupon', 'px_last': 'last_price', 'security_typ': 'security_type'}
COLS = ['market_cds_ticker', 'equity_ticker', 'security_id', 'security_name', 'last_price', 'asset_class', 'currency', 'issue_date', 'maturity',
        'notional_outstanding', 'notional_local', 'notional_total', 'amt_out_asof', 'coupon', 'payment_rank', 'security_type', 'pulled_at']
KEY = ['market_cds_ticker', 'security_id']
MTN_TYPES = ['DOMESTIC MTN', 'EURO MTN']                      # security_typ values dropped (medium-term notes); preferreds / other asset classes dropped by the asset-class filter
MAX_PER_PARENT = 100                                          # keep the largest N securities (USD notional) per parent: for huge capital structures the big picture is what matters
EXCLUDE_FILE = ROOT / 'config' / 'debt_exclude.csv'            # hand-maintained: names skipped (banks / broker-dealers: tens of thousands of notes each)


def universe():
    """market_cds_ticker -> parent equity ticker for every mappings row that has one (a name with no public equity cannot be queried)."""
    m = pd.read_csv(b.MAPPINGS, dtype=str, encoding='cp1252').drop_duplicates('CDS_Ticker')
    m['equity_ticker'] = [b.equity_ticker(t) for t in m.CDS_Ticker]
    return m[['CDS_Ticker', 'equity_ticker']].rename(columns={'CDS_Ticker': 'market_cds_ticker'}).dropna(subset=['market_cds_ticker'])


def query(e):
    """BQL universe for one parent: bonds + loans (no preferreds), medium-term notes dropped."""
    mtn = ','.join(f"'{t}'" for t in MTN_TYPES)
    return f"filter(debt('{e}', issuedby='{b.ISSUEDBY}'), srch_asset_class in ['Corporates','Loans'] and not(security_typ in [{mtn}]))"


def alternates():
    """market_cds_ticker -> fallback equity tickers (ultimate parent, then the public-equity entity), used only for names whose parent ticker returns nothing."""
    m = pd.read_csv(b.MAPPINGS, dtype=str, encoding='cp1252').drop_duplicates('CDS_Ticker').set_index('CDS_Ticker')
    bad = ('', '--', 'NaN', 'nan')
    return {t: [v.strip() for c in ('Ultimate_Parent_Equity_Ticker', 'CDS_Public_Equity_Entity_Ticker') if isinstance(v := r.get(c), str) and v.strip() not in bad]
            for t, r in m.iterrows()}


def pull(eqs, parts, tag, batch, timeout, log, errors):
    """One filtered BQL per equity ticker, in batches; finished batches are cached in `parts` (named by parent list + field list). Returns the part files."""
    files, t0 = [], time.time()
    fh = hashlib.md5(FIELDS.encode()).hexdigest()[:4]
    for i in range(0, len(eqs), batch):
        chunk = eqs[i:i + batch]
        f = parts / f'{tag}_{hashlib.md5(",".join(chunk).encode()).hexdigest()[:10]}_{fh}.parquet'      # named by its parent list and fields, so a stale part is never reused
        files.append(f)
        if f.exists() and len(pd.read_parquet(f)):                              # an empty part (all errors) is retried
            continue
        out, err = b.bql_many({e: query(e) for e in chunk}, FIELDS, timeout=timeout, log=lambda *_: None)
        if any('Capacity' in str(m) or 'limits exceeded' in str(m) for m in err.values()):      # Bloomberg's daily / monthly data limit: stop now; rerun later (finished batches are kept)
            log(f'STOPPED at {tag} batch {i // batch + 1}: Bloomberg data limit reached ({next(iter(err.values()))}). Finished batches are kept; rerun `run` after the limit resets.')
            raise SystemExit(1)
        errors.update(err)
        df = pd.concat([d.assign(equity_ticker=e) for e, d in out.items()], ignore_index=True) if out else pd.DataFrame()
        df.to_parquet(f, index=False)
        log(f'{tag} batch {i // batch + 1}/{-(-len(eqs) // batch)}: {len(chunk)} parents, {len(df):,} rows, {len(err)} errors  [{(time.time() - t0) / 60:.1f} min]')
    return files


def run(batch=20, timeout=900, limit=None, log=print):
    today = pd.Timestamp.today().normalize()
    parts = OUT / 'parts' / f'{today:%Y-%m-%d}'
    parts.mkdir(parents=True, exist_ok=True)
    uni = universe()
    skipped = uni[uni.equity_ticker.isna()].market_cds_ticker.tolist()
    uni = uni.dropna(subset=['equity_ticker'])
    if EXCLUDE_FILE.exists():
        ex = set(pd.read_csv(EXCLUDE_FILE, dtype=str).market_cds_ticker)
        log(f'excluding {len(ex)} names listed in {EXCLUDE_FILE.name}')
        uni = uni[~uni.market_cds_ticker.isin(ex)]
    eqs = sorted(uni.equity_ticker.unique())[:limit]
    log(f'{len(uni)} names with an equity ticker -> {len(eqs)} unique parents to query ({len(skipped)} names have no equity ticker: {skipped})')
    errors = {}
    files = pull(eqs, parts, 'part', batch, timeout, log, errors)
    allp = pd.concat([pd.read_parquet(p) for p in files], ignore_index=True)
    if allp.empty:
        raise SystemExit('no rows returned for any parent; nothing written')

    # fallback: names whose parent ticker returned nothing are retried with their own BBG_Equity_Ticker / public-equity ticker (the parent stays the default for everyone else)
    empty = sorted(set(eqs) - set(allp.equity_ticker))
    if empty:
        alt = alternates()
        fb = {t: [a for a in alt.get(t, []) if a != e and a not in eqs] for t, e in zip(uni.market_cds_ticker, uni.equity_ticker) if e in empty}
        alts = sorted({a for v in fb.values() for a in v})
        log(f'{len(empty)} parents returned nothing; retrying {sum(1 for v in fb.values() if v)} names with {len(alts)} alternate tickers')
        if alts:
            fp = pull(alts, parts, 'fallback', batch, timeout, log, errors)
            alt_rows = pd.concat([pd.read_parquet(p) for p in fp], ignore_index=True)
            got = set(alt_rows.equity_ticker) if len(alt_rows) else set()
            use = {t: next((a for a in v if a in got), None) for t, v in fb.items()}
            use = {t: a for t, a in use.items() if a}
            uni = uni.assign(equity_ticker=[use.get(t, e) for t, e in zip(uni.market_cds_ticker, uni.equity_ticker)])
            allp = pd.concat([allp, alt_rows], ignore_index=True)
            log(f'fallback found debt for {len(use)} names: ' + ', '.join(f'{t}->{a}' for t, a in list(use.items())[:12]))
    allp = allp.rename(columns=RENAME).drop(columns='ID', errors='ignore')
    allp = allp[allp.maturity.isna() | (allp.maturity >= today)]                 # drop instruments that have already matured (perpetuals have no maturity and stay)
    allp = allp.sort_values('notional_outstanding', ascending=False, na_position='last').groupby('equity_ticker', sort=False).head(MAX_PER_PARENT)
    allp['notional_total'] = allp.tranche_size.where(allp.asset_class.eq('Loans'), allp.notional_local)   # loans: facility (tranche) size; bonds can't be redrawn, so total = amount outstanding
    snap = uni.merge(allp, on='equity_ticker', how='inner')                     # copy each parent's rows to every CDS name that maps to it
    snap['pulled_at'] = pd.Timestamp.now().floor('s')
    snap = snap[COLS].drop_duplicates(KEY).reset_index(drop=True)
    path = OUT / f'debt_{today:%Y-%m-%d}.parquet'
    snap.to_parquet(path, index=False)
    missing = sorted(set(uni.market_cds_ticker) - set(snap.market_cds_ticker))
    log(f'wrote {path}: {len(snap):,} securities, {snap.market_cds_ticker.nunique()} names; no rows for {len(missing)} names' + (f' (e.g. {missing[:10]})' if missing else ''))
    if errors:
        pd.Series(errors, name='error').rename_axis('equity_ticker').to_csv(OUT / f'errors_{today:%Y-%m-%d}.csv')
        log(f'{len(errors)} queries returned an Excel error, see errors_{today:%Y-%m-%d}.csv')
    return snap


def diff(new=None, old=None):
    """Compare two snapshot files (default: the newest two). Returns {'added', 'removed', 'changed'} DataFrames and prints a summary."""
    files = sorted(OUT.glob('debt_*.parquet'))
    if new is None or old is None:
        if len(files) < 2:
            raise SystemExit('need at least two snapshots to compare')
        old, new = files[-2], files[-1]
    a, c = pd.read_parquet(old), pd.read_parquet(new)
    m = a.merge(c, on=KEY, how='outer', suffixes=('_old', '_new'), indicator=True)
    added, removed = m[m._merge == 'right_only'], m[m._merge == 'left_only']
    both = m[m._merge == 'both']
    chg = both[(both.notional_outstanding_old.fillna(-1) != both.notional_outstanding_new.fillna(-1)) | (both.maturity_old.fillna(pd.Timestamp(0)) != both.maturity_new.fillna(pd.Timestamp(0)))]
    print(f'{pathlib.Path(old).name} -> {pathlib.Path(new).name}: {len(a):,} -> {len(c):,} securities | added {len(added):,}, removed {len(removed):,}, changed amount/maturity {len(chg):,}')
    for label, d in (('added', added), ('removed', removed), ('changed', chg)):
        top = d.groupby('market_cds_ticker').size().sort_values(ascending=False).head(10)
        print(f'  {label} by name:', ', '.join(f'{t} {n}' for t, n in top.items()) or '-')
    return {'added': added, 'removed': removed, 'changed': chg}


if __name__ == '__main__':
    a = sys.argv[1:]
    if a and a[0] == 'diff':
        diff()
    else:
        run(limit=int(a[a.index('--limit') + 1]) if '--limit' in a else None)
