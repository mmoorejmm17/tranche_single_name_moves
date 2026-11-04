"""Publish the working copy (cache/, config/) as the frozen snapshot the analysis reads (data/). Owner only.

    python pricing/shared/publish.py            # recompute index_metrics from the working copy, check it, copy into data/, write data/MANIFEST.json
    python pricing/shared/publish.py --check    # only run the checks
    python pricing/shared/publish.py --allow-partial   # publish even if the newest date is mostly empty (normally refused)
    python pricing/shared/publish.py --restore  # put the previous published store (data/_previous, kept by every publish) back

Before anything is overwritten the current published store is copied to data/_previous (one level of undo).
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


ALLOW_PARTIAL = '--allow-partial' in sys.argv
PREVIOUS = DATA.parent / '_previous'          # data/_previous: the store as it was before the latest publish (one level of undo; never read by the analysis)


def _backup_previous():
    """Keep the currently published store in data/_previous before it is overwritten (replaces any older backup)."""
    if not (DATA / 'MANIFEST.json').exists():
        return
    tmp = DATA.parent / '_previous.tmp'
    if tmp.exists():
        shutil.rmtree(tmp)
    shutil.copytree(DATA, tmp)
    if PREVIOUS.exists():
        shutil.rmtree(PREVIOUS)
    tmp.rename(PREVIOUS)
    old = json.loads((PREVIOUS / 'MANIFEST.json').read_text(encoding='utf-8'))
    print(f"previous published store kept in data/_previous (published {old.get('published_at')}, prices through {old.get('store_through')})")


def restore_previous():
    """Put data/_previous back as the published store (the files are copied over data/base one at a time; MANIFEST.json last)."""
    if not (PREVIOUS / 'MANIFEST.json').exists():
        raise FileNotFoundError(f'{PREVIOUS} has no previous store to restore')
    files = [p for p in PREVIOUS.rglob('*') if p.is_file() and p.name != 'MANIFEST.json']
    for src in files:
        dst = DATA / src.relative_to(PREVIOUS)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + '.tmp')
        shutil.copy2(src, tmp)
        tmp.replace(dst)
    keep = {p.relative_to(PREVIOUS) for p in files}
    for p in [p for p in DATA.rglob('*') if p.is_file() and p.name != 'MANIFEST.json' and p.relative_to(DATA) not in keep]:
        p.unlink()                                  # files that only exist in the version being replaced (e.g. overrides added since)
    shutil.copy2(PREVIOUS / 'MANIFEST.json', DATA / 'MANIFEST.json')
    old = json.loads((DATA / 'MANIFEST.json').read_text(encoding='utf-8'))
    print(f"restored the previous published store (published {old.get('published_at')}, prices through {old.get('store_through')})")


def _check():
    missing = [f for f in REQUIRED if not (STORE / f).exists()]
    if missing:
        raise FileNotFoundError(f'cache/base is missing {missing}; build them first (cdx_store.update_store, cds_pricer, addin_fill)')
    st = pd.read_parquet(STORE / 'prices' / 'spreads.parquet', columns=['index_name', 'published_date'])
    names = list(pd.read_csv(CONFIG / 'cds_index.csv')['index_name'])
    absent = [n for n in names if n not in set(st['index_name'])]
    if absent:
        raise ValueError(f'store has no rows for {absent}')
    last = st['published_date'].max()
    cov = pd.read_parquet(STORE / 'prices' / 'spreads.parquet', columns=['published_date', 'has_data'])
    pct = cov.loc[cov['published_date'] == last, 'has_data'].mean() * 100
    if pct < 50 and not ALLOW_PARTIAL:
        raise ValueError(f'the newest date {last:%Y-%m-%d} has Databricks rows for only {pct:.0f}% of its cells (not loaded yet?). Publishing it would show nearly every name as missing. '
                         f'Run pricing/daily/update.py later (it drops such a date), or pass --allow-partial to publish anyway.')
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
    _backup_previous()
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
    if '--restore' in sys.argv:
        restore_previous()
    else:
        publish(check_only='--check' in sys.argv)
