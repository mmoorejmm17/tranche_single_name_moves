"""Fill the cells Databricks could not price from the Markit Excel add-in (waterfall source 2), at each index's own coupon.

    python src/addin_fill.py plan       # size the job, nothing is pulled
    python src/addin_fill.py pull       # pull every needed (ticker, node, coupon) through Excel; resumable (cache/addin_raw/full_*.parquet)
    python src/addin_fill.py assemble   # interpolate half-year tenors, derive what the add-in lacks with cds_pricer, write cache/addin_fill.parquet
Run with the system Python (needs Excel + the add-in login for `pull`, the Quantifi Toolkit for `assemble`).

A cell is 'unpriced' by cdx_spreads.priced_mask. Its tenor label comes from Databricks (markit_addin.tenor_map); an annual label is one add-in node, a half-year label
(2Y6M) is the 50/50 average of the two bracketing annual nodes for conv_spread, par_spread and risky_pv01 (upfront is not interpolated, it is derived).
A cell is FILLED if every node it needs has a par_spread, or a conv_spread, or an upfront at the index coupon (the agreed definition of priced).
risky_pv01 is the add-in's where present (it only exists from 2025-09-01), otherwise derived by cds_pricer from a flat curve.
"""
import hashlib
import os
import sys
import time

import numpy as np
import pandas as pd

import cdx_spreads as cs
import markit_addin as ma

FILL_FILE = ma.CACHE / 'addin_fill.parquet'
DEFAULTED_FILL_FILE = ma.CACHE / 'addin_fill_defaulted.parquet'
BATCH_IDS = 40


def unpriced_cells():
    """Every unpriced (index, member, date) cell with the tenor label, the add-in nodes it needs and the ids of those nodes."""
    cols = ['index_name', 'index_coupon', 'member_name', 'market_cds_ticker', 'published_date', 'cds_maturity', 'has_data', 'running_coupon',
            'par_spread', 'upfront', 'conv_spread']
    st = pd.read_parquet(cs.STORE_FILE, columns=cols)
    un = st[~cs.priced_mask(st)].drop(columns=['has_data', 'running_coupon', 'par_spread', 'upfront', 'conv_spread']).reset_index(drop=True)
    tm = ma.tenor_map().drop_duplicates(['published_date', 'cds_maturity'])
    un = un.merge(tm, on=['published_date', 'cds_maturity'], how='left')
    return un


def defaulted_cells():
    """Cells for names that defaulted out of a series (teams.structured_credit_abhutra.credit_events): every date from the series launch to the day before index_ex_date
    on which Databricks has no all_upfront for the name at the series maturity (see index_metrics.refresh_defaulted). Same columns as unpriced_cells()."""
    import index_metrics as im
    ev = pd.read_parquet(im.EVENTS_FILE)
    have = pd.read_parquet(im.DEFAULTED_FILE)
    done = set(zip(have.index_name, have.market_cds_ticker, have.published_date))
    tm = ma.tenor_map().drop_duplicates(['published_date', 'cds_maturity'])
    rows = []
    for e in ev.itertuples():
        for d in sorted(cs.get_spread_history(e.index_name)['published_date'].unique()):
            if d < e.index_ex_date and (e.index_name, e.sn_ticker, d) not in done:
                rows.append((e.index_name, cs.index_coupon(e.index_name), f'{e.sn_ticker} (defaulted)', e.sn_ticker, d, e.maturity))
    c = pd.DataFrame(rows, columns=['index_name', 'index_coupon', 'member_name', 'market_cds_ticker', 'published_date', 'cds_maturity'])
    return c.merge(tm, on=['published_date', 'cds_maturity'], how='left')


def expand_nodes(cells):
    """One row per (cell, node) with weight and add-in id; cells without a tenor label or a mapping id are returned separately."""
    cells = cells.copy()
    cells['cid'] = np.arange(len(cells))
    labelled = cells[cells.tenor.notna()]
    plan = pd.DataFrame([(t, n, w) for t in labelled.tenor.unique() for n, w in ma.node_plan(t)], columns=['tenor', 'node', 'w'])
    nd = labelled[['cid', 'market_cds_ticker', 'index_coupon', 'published_date', 'tenor']].merge(plan, on='tenor')
    alias = nd.market_cds_ticker.isin(ma.alias_tickers())      # renamed curves (ticker_aliases.csv) need the id valid on each date
    ids = {k: ma.build_id(*k) for k in nd.loc[~alias, ['market_cds_ticker', 'node', 'index_coupon']].drop_duplicates().itertuples(index=False, name=None)}
    nd['id'] = [None if a else ids[k] for a, k in zip(alias, zip(nd.market_cds_ticker, nd.node, nd.index_coupon))]
    if alias.any():
        nd.loc[alias, 'id'] = [ma.build_id(t, n, c, d) for t, n, c, d in
                               zip(nd.loc[alias, 'market_cds_ticker'], nd.loc[alias, 'node'], nd.loc[alias, 'index_coupon'], nd.loc[alias, 'published_date'])]
    no_id = set(nd.loc[nd.id.isna(), 'cid'])
    return cells, nd[nd.id.notna() & ~nd.cid.isin(no_id)], no_id


def plan_batches(nd, prefix='full'):
    inst = nd.groupby(['market_cds_ticker', 'id']).published_date.agg(['min', 'max']).reset_index()
    batches, cur, k = [], [], 0

    def flush():
        nonlocal cur, k
        if cur:
            ids, a, b = [i for i, _, _ in cur], min(a for _, a, _ in cur), max(b for _, _, b in cur)
            tag = hashlib.md5((''.join(ids) + str(a) + str(b)).encode()).hexdigest()[:8]      # name by content so a changed plan never reuses a stale cache file
            batches.append((f'{prefix}_{k:03d}_{tag}', ids, a, b))
            k += 1
            cur = []
    for _, g in inst.groupby('market_cds_ticker'):
        items = [(r.id, r.min, r.max) for r in g.itertuples()]
        if len(cur) + len(items) > BATCH_IDS:
            flush()
        cur += items
    flush()
    return inst, batches


def plan(cells=None):
    cells = unpriced_cells() if cells is None else cells
    print(f'unpriced cells: {len(cells):,} | without a tenor label: {cells.tenor.isna().sum():,}')
    cells, nd, no_id = expand_nodes(cells)
    inst, batches = plan_batches(nd)
    print(f'cells with no mapping id (e.g. COX): {len(no_id):,} | cell-node rows: {len(nd):,} | instruments: {len(inst):,} | batches of <= {BATCH_IDS}: {len(batches)}')
    rows = sum(len(ids) * ((b - a).days + 1) for _, ids, a, b in batches)
    print(f'rows the add-in will return (every day in each batch range): about {rows:,}')
    return cells, nd, inst, batches


def pull(cells=None):
    cells, nd, inst, batches = plan(cells)
    have = set(ma.load_raw().id.unique()) if any(ma.RAW_DIR.glob('*.parquet')) else set()
    nd_new = nd[~nd.id.isin(have)]
    if len(nd_new) < len(nd):
        inst, batches = plan_batches(nd_new)
        print(f'already pulled: {len(have)} ids; still to pull: {len(inst)} instruments in {len(batches)} batches')
    t0 = time.time()
    with ma.AddinSession() as sess:
        errors = ma.pull_batches(batches, sess)
    print(f'pull finished in {(time.time() - t0) / 60:.1f} min; failed ids: {len(errors)}')
    pd.Series(errors, dtype=str).to_csv(ma.CACHE / 'addin_fill_errors.csv')


def assemble(cells=None, out_file=FILL_FILE, write_unfilled=True):
    import cds_pricer as cp
    cells, nd, no_id = expand_nodes(unpriced_cells() if cells is None else cells)
    raw = ma.load_raw().drop_duplicates(['id', 'date'])
    nd = nd.merge(raw.rename(columns={'date': 'published_date'}), on=['id', 'published_date'], how='left')
    nd['conv_ok'] = nd.conv.notna() & (nd.conv > 0)
    nd['par_ok'] = nd.par.notna() & (nd.par > 0)
    nd['up_ok'] = nd.upfront.notna()
    nd['rpv_ok'] = nd.rpv01.notna() & (nd.rpv01 > 0)
    for c in ('conv', 'par', 'rpv01', 'upfront'):
        nd[c + '_w'] = nd[c] * nd.w
    g = nd.groupby('cid')
    agg = pd.DataFrame({'n_nodes': g.size(), 'conv_ok': g.conv_ok.sum(), 'par_ok': g.par_ok.sum(), 'up_ok': g.up_ok.sum(), 'rpv_ok': g.rpv_ok.sum(),
                        'conv_i': g.conv_w.sum(min_count=1), 'par_i': g.par_w.sum(min_count=1), 'rpv_i': g.rpv01_w.sum(min_count=1),
                        'up_i': g.upfront_w.sum(min_count=1), 'ccy': g.id.first().str.split(ma.BS).str[2]})
    c = cells.join(agg, on='cid')
    full = lambda k: (c[k + '_ok'] == c.n_nodes) & c.n_nodes.notna()
    c['filled'] = full('par') | full('conv') | full('up')
    f = c[c.filled].copy()
    f['conv_in'] = f.conv_i.where(full('conv'))
    f['par_in'] = f.par_i.where(full('par'))
    f['rpv_in'] = f.rpv_i.where(full('rpv'))
    f['up_in'] = f.up_i.where((f.n_nodes == 1) & full('up'))        # upfront is only taken from the add-in for an annual tenor
    print(f'filled {len(f):,} of {len(c):,} unpriced cells ({len(f) / len(c):.1%}); with add-in risky_pv01: {f.rpv_in.notna().sum():,}')
    pts = pd.DataFrame({'published_date': f.published_date, 'cds_maturity': f.cds_maturity, 'currency': f.ccy, 'running_coupon': f.index_coupon.astype(float),
                        'par_spread': f.par_in, 'risky_pv01': f.rpv_in, 'upfront': f.up_in, 'conv_spread': f.conv_in}, index=f.index)
    t0 = time.time()
    o = cp.price_rows(pts, f.index_coupon.values)
    print(f'pricer: {len(o):,} points in {(time.time() - t0) / 60:.1f} min | routes {o.route.value_counts().to_dict()} | conv_status {o.conv_status.value_counts(dropna=False).to_dict()}')
    # Near-default points (par spread above ~25,000 bp) can make the flat-curve fit fail, which would drop the name exactly when it is widest. There the all-in upfront is the
    # add-in's own upfront plus coupon x a credit-triangle risky duration (about a point), so use that and flag it.
    # A par spread above ~1,100 bp makes the par-based all_upfront (par x flat-curve risky_pv01) saturate near 60 (the most a 40%-recovery model can give), while the traded
    # price, the upfront, keeps rising (71-100 points just before default). So for wide cells, and where the fit failed, all_upfront = the add-in's own upfront + coupon x
    # risky_pv01 (the pricer's, or a credit-triangle approximation when the fit failed), which is the real quote.
    has_up = (f['up_ok'] == f['n_nodes'])
    bad = o['all_upfront'].isna() & has_up
    wide = (o['all_upfront'] > 50) & has_up
    fix = bad | wide
    if fix.any():
        T = (f['cds_maturity'] - f['published_date']).dt.days / 365.0
        lam = (f['par_in'].fillna(f['conv_in']) / 1e4) / 0.6
        rpv_t = pd.Series(np.where(lam.notna(), (1 - np.exp(-(lam + 0.04) * T)) / (lam + 0.04), 0.2), index=f.index)
        rpv_use = o['risky_pv01'].where(~bad & o['risky_pv01'].notna(), rpv_t)
        o.loc[fix, 'upfront'] = f.loc[fix, 'up_i']
        o.loc[fix, 'risky_pv01'] = rpv_use[fix]
        o.loc[fix, 'all_upfront'] = f.loc[fix, 'up_i'] + f.loc[fix, 'index_coupon'] * rpv_use[fix] / 100.0
        o.loc[fix, 'route'] = 'addin upfront (wide / fit failed)'
        print(f'wide or failed cells priced from the add-in upfront: {int(fix.sum()):,} ({int(bad.sum()):,} fit failures, {int(wide.sum()):,} with par-based all_upfront > 50); still without all_upfront: {int(o["all_upfront"].isna().sum()):,}')
    out = pd.DataFrame({
        'index_name': f.index_name, 'market_cds_ticker': f.market_cds_ticker, 'member_name': f.member_name, 'published_date': f.published_date,
        'cds_maturity': f.cds_maturity, 'index_coupon': f.index_coupon, 'tenor': f.tenor, 'n_nodes': f.n_nodes,
        'conv_spread': f.conv_in.fillna(o.conv_spread), 'par_spread': f.par_in.fillna(o.par_spread), 'risky_pv01': o.risky_pv01,
        'upfront': o.upfront, 'all_upfront': o.all_upfront, 'conv_status': o.conv_status, 'recovery_used': o.recovery_used,
        'route': o.route, 'rpv01_source': np.where(f.rpv_in.notna(), 'data', 'derived'), 'source': 'markit_addin'})
    out.to_parquet(out_file, index=False)
    print(f'wrote {out_file}: {len(out):,} cells')
    # coverage report
    c['family'] = c.index_name.str[:2]
    rep = c.groupby('family').agg(unpriced=('filled', 'size'), filled=('filled', 'sum'))
    rep['filled %'] = rep.filled / rep.unpriced * 100
    print(rep.round(1).to_string())
    left = c[~c.filled]
    top = left.groupby('market_cds_ticker').size().sort_values(ascending=False).head(15)
    print(f'still unpriced: {len(left):,} cells in {left.market_cds_ticker.nunique()} tickers; top:', top.to_dict())
    if write_unfilled:
        left[['index_name', 'market_cds_ticker', 'member_name', 'published_date', 'cds_maturity', 'index_coupon', 'tenor']].to_parquet(ma.CACHE / 'addin_unfilled.parquet', index=False)
    return c


if __name__ == '__main__':
    cmd = sys.argv[1]
    if cmd == 'plan-defaulted':
        plan(defaulted_cells())
    elif cmd == 'pull-defaulted':
        pull(defaulted_cells())
    elif cmd == 'assemble-defaulted':
        assemble(defaulted_cells(), DEFAULTED_FILL_FILE, write_unfilled=False)
    else:
        {'plan': plan, 'pull': pull, 'assemble': assemble}[cmd]()
