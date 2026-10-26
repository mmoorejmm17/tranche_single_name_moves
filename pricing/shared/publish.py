"""Publish the working copy (cache/, config/) as the frozen snapshot the analysis reads (data/). Owner only.

    python pricing/shared/publish.py            # recompute index_metrics from the working copy, check it, copy into data/, write data/MANIFEST.json
    python pricing/shared/publish.py --check    # only run the checks

Each file is copied to a temp name and renamed into place, and MANIFEST.json is written LAST, so a reader sees either the old snapshot or the new one
per file; publish while nobody is running the notebooks. Needs only pandas / pyarrow (the venv is fine).
"""
import _paths  # noqa: F401
import datetime as dt
import json
import shutil
import sys

import pandas as pd

import cdx_spreads as cs
import index_metrics as im
from _paths import CONFIG, DATA, STORE

# the store layout (same in cache/base and data/base): prices/ reference/ metrics/ MANIFEST.json
REQUIRED = ['prices/spreads.parquet', 'prices/priced_at_index_coupon.parquet', 'prices/addin_fill.parquet', 'reference/index_maturity.csv']
OPTIONAL = ['prices/overrides_priced.parquet', 'prices/addin_fill_defaulted.parquet', 'prices/recalibrated.parquet', 'reference/credit_events.parquet',
            'reference/defaulted_prices.parquet']
FROM_CONFIG = {'cds_index.csv': True, 'overrides.csv': False}      # name -> required; copied into reference/


def _check():
    missing = [f for f in REQUIRED if not (STORE / f).exists()]
    if missing:
        raise FileNotFoundError(f'cache/base is missing {missing}; build them first (cdx_store.update_store, cds_pricer, addin_fill)')
    st = pd.read_parquet(STORE / 'prices' / 'spreads.parquet', columns=['index_name', 'published_date'])
    names = list(pd.read_csv(CONFIG / 'cds_index.csv')['index_name'])
    absent = [n for n in names if n not in set(st['index_name'])]
    if absent:
        raise ValueError(f'store has no rows for {absent}')
    for n in names:
        if not (STORE / 'reference' / 'members' / f'members_{n}.csv').exists():
            raise FileNotFoundError(f'cache/base/reference/members/members_{n}.csv missing')
    return st['published_date'].max()


def publish(check_only=False):
    through = _check()
    print(f'checks ok; store runs through {through:%Y-%m-%d}')
    if check_only:
        return
    (STORE / 'metrics').mkdir(exist_ok=True)
    metrics = im.all_index_metrics(out_file=STORE / 'metrics' / 'index_metrics.parquet')       # recomputed from the working copy so it always matches it
    assert not metrics.duplicated(['index_name', 'published_date']).any()
    for d in ('prices', 'reference/members', 'metrics'):
        (DATA / d).mkdir(parents=True, exist_ok=True)
    jobs = [(STORE / f, f) for f in REQUIRED + ['metrics/index_metrics.parquet']]
    jobs += [(STORE / f, f) for f in OPTIONAL if (STORE / f).exists()]
    jobs += [(p, f'reference/members/{p.name}') for p in sorted((STORE / 'reference' / 'members').glob('members_*.csv'))]
    for f, req in FROM_CONFIG.items():
        if (CONFIG / f).exists():
            jobs.append((CONFIG / f, f'reference/{f}'))
        elif req:
            raise FileNotFoundError(CONFIG / f)
    files = {}
    for src, name in jobs:
        tmp = DATA / (name + '.tmp')
        shutil.copy2(src, tmp)
        tmp.replace(DATA / name)
        files[name] = (DATA / name).stat().st_size
    for rel, src in (('reference/overrides.csv', CONFIG / 'overrides.csv'), ('prices/overrides_priced.parquet', STORE / 'prices' / 'overrides_priced.parquet')):
        if not src.exists() and (DATA / rel).exists():          # overrides removed on the working side: remove from the published store too
            (DATA / rel).unlink()
    manifest = {'store': 'base', 'published_at': dt.datetime.now().strftime('%Y-%m-%d %H:%M'), 'store_through': f'{through:%Y-%m-%d}', 'files': files}
    tmp = DATA / 'MANIFEST.json.tmp'
    tmp.write_text(json.dumps(manifest, indent=1), encoding='utf-8')
    tmp.replace(DATA / 'MANIFEST.json')
    print(f"published {len(files)} files ({sum(files.values()) / 1e6:,.0f} MB) to data/base, through {through:%Y-%m-%d}")


if __name__ == '__main__':
    publish(check_only='--check' in sys.argv)
