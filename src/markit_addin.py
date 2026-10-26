"""Pull single-name CDS curve points from the Markit Excel add-in (IHSMTimeseries), in bulk, for the cells Databricks could not price.

The add-in lives in Excel, so this drives a separate Excel window with xlwings (system Python; the project .venv has no pywin32 / Quantifi).
You must be logged in to the add-in once (IHS Markit ribbon > Login); the login persists across Excel windows. Never script credentials.

What the add-in gives: standard tenor nodes only (6M, 1Y, 2Y ... 10Y; no half-years), at a stated coupon. A cell whose Databricks tenor label is a half-year
(e.g. 2Y6M) is therefore built from the two bracketing annual nodes, 50/50, linearly in conv_spread, par_spread and risky_pv01 (the same rule
Databricks' own EOD-Interp rows follow, and the one agreed with the user). Upfront is not interpolated (it is not a plain average); derive it with cds_pricer.

Hard-won notes: do NOT call Calculate() while waiting (it restarts the add-in's async fetch and the result never lands); wait for the 'IHSM [RxC]' marker in the
formula cell and then for the array to be written below it. Limits per call: 50 instruments, 20 years, instruments x fields x days <= 500,000.
"""
import os
import re
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent      # project root (this file lives in src/)
CACHE = ROOT / 'cache'
RAW_DIR = CACHE / 'addin_raw'
TENOR_MAP_FILE = CACHE / 'tenor_map.parquet'
MAPPINGS = 'S:/Structured Credit/Matt Stuff/cds_mappings/cds_mappings_final.csv'
XLL = os.path.normpath(os.path.expandvars('%LOCALAPPDATA%/Programs/IHS Markit/IHS Markit Add-In for Microsoft Excel/ihsmExcel6x64.xll'))
FIELDS = {'asOf': 'asOf', 'conv': 'curve.curvePoints.conventionalSpread', 'upfront': 'curve.curvePoints.upfront',
          'rpv01': 'curve.curvePoints.riskyPv01', 'par': 'curve.curvePoints.parSpread'}
BS = chr(92)
HISTORY_START = '2022-04-01'


# ---- tenor labels -----------------------------------------------------------------------------------------------------------------
def tenor_map(refresh=False):
    """(published_date, cds_maturity) -> Databricks tenor label (6M, 1Y, 1Y6M ...), cached. This is the authoritative 'which tenor is the index maturity on that date'."""
    if TENOR_MAP_FILE.exists() and not refresh:
        return pd.read_parquet(TENOR_MAP_FILE)
    sys.path.insert(0, 'S:/Structured Credit/Matt Stuff/claude_repos/databricks_utils')
    from databricks_utils._connection import get_conn
    conn = get_conn()
    try:
        df = pd.read_sql("SELECT DISTINCT published_date, cds_maturity, tenor FROM teams.structured_credit_abhutra.cds_transform_interpolated "
                         f"WHERE published_date >= '{HISTORY_START}'", conn)
    finally:
        conn.close()
    df['published_date'] = pd.to_datetime(df['published_date'])
    df['cds_maturity'] = pd.to_datetime(df['cds_maturity'])
    CACHE.mkdir(exist_ok=True)
    df.to_parquet(TENOR_MAP_FILE, index=False)
    return df


def node_plan(label):
    """Databricks tenor label -> [(add-in node, weight)]. Annual labels map to themselves; 'xY6M' is 50/50 between xY and (x+1)Y."""
    m = re.fullmatch(r'(\d+)Y6M', label)
    if m:
        y = int(m.group(1))
        return [(f'{y}Y', 0.5), (f'{y + 1}Y', 0.5)]
    if re.fullmatch(r'(\d+)Y|6M', label):
        return [(label, 1.0)]
    raise ValueError(f'unexpected tenor label {label!r}')


# ---- identifiers ------------------------------------------------------------------------------------------------------------------
_MAP = None


ALIAS_FILE = ROOT / 'config' / 'ticker_aliases.csv'     # hand-maintained: Markit renamed / re-issued a curve (e.g. Cox Communications on 2026-09-15)
_ALIASES = None


DELTA_FILE = Path('S:/Structured Credit/Matt Stuff/cds_mappings/markit_ticker_delta_final.csv')   # the user's own record of Markit ticker changes


def _aliases():
    """market_cds_ticker -> [(valid_from, valid_to, markit_ticker)].

    Built from the user's markit_ticker_delta_final.csv (change_date, market_cds_ticker, new_markit_ticker, old_markit_ticker): the old ticker applies before
    change_date, the new one from change_date on (a chain of changes is handled in date order). ticker_aliases.csv adds or replaces entries for changes that
    file does not have yet (blank dates are open-ended); a ticker listed there uses only those rows."""
    global _ALIASES
    if _ALIASES is None:
        _ALIASES = {}
        if DELTA_FILE.exists():
            d = pd.read_csv(DELTA_FILE, dtype=str).fillna('')
            for t, g in d.groupby('market_cds_ticker'):
                g = g.assign(cdate=pd.to_datetime(g['change_date'])).sort_values('cdate')
                segs = [(pd.Timestamp.min, g.iloc[0].cdate - pd.Timedelta(days=1), g.iloc[0].old_markit_ticker)]
                for i, r in enumerate(g.itertuples()):
                    end = g.iloc[i + 1].cdate - pd.Timedelta(days=1) if i + 1 < len(g) else pd.Timestamp.max
                    segs.append((r.cdate, end, r.new_markit_ticker))
                _ALIASES[t] = segs
        if ALIAS_FILE.exists():
            a = pd.read_csv(ALIAS_FILE, dtype=str).fillna('')
            for t in a.market_cds_ticker.unique():
                _ALIASES[t] = []
            for r in a.itertuples():
                _ALIASES[r.market_cds_ticker].append(
                    (pd.Timestamp(r.valid_from) if r.valid_from else pd.Timestamp.min, pd.Timestamp(r.valid_to) if r.valid_to else pd.Timestamp.max, r.markit_ticker))
    return _ALIASES


def alias_tickers():
    return set(_aliases())


IDS_FILE = ROOT / 'config' / 'ticker_ids.csv'     # hand-maintained: Markit ticker / tier / currency / doc clause for names that are not in cds_mappings_final.csv
_MANUAL = None


def _manual_ids():
    """market_cds_ticker -> (markit_ticker, tier, ccy, doc) from ticker_ids.csv."""
    global _MANUAL
    if _MANUAL is None:
        _MANUAL = {}
        if IDS_FILE.exists():
            for r in pd.read_csv(IDS_FILE, dtype=str).fillna('').itertuples():
                _MANUAL[r.market_cds_ticker] = (r.markit_ticker, r.tier, r.ccy, r.doc)
    return _MANUAL


def build_id(ticker, node, coupon, date=None):
    """Markit_Ticker\\Tier\\Ccy\\DocClause\\Tenor\\Coupon for the add-in; None if the ticker can't be built.

    Tier / currency / doc clause come from the mappings file's Markit_5Y_ID, or from its Markit_Seniority / Markit_Currency / Markit_Doc_Clause columns when that ID is
    missing (e.g. COX). A ticker listed in ticker_aliases.csv gets the Markit ticker that was valid on `date` (pass the date for those tickers)."""
    global _MAP
    if _MAP is None:
        _MAP = pd.read_csv(MAPPINGS, dtype=str, encoding='cp1252').drop_duplicates('CDS_Ticker').set_index('CDS_Ticker')
    if ticker not in _MAP.index:
        man = _manual_ids().get(ticker)                      # names no longer in the mappings file (e.g. defaulted ones): ticker_ids.csv
        return BS.join([man[0], man[1], man[2], man[3], node, str(int(coupon))]) if man else None
    r = _MAP.loc[ticker]
    base = r['Markit_5Y_ID']
    if isinstance(base, str) and base:
        mk, tier, ccy, doc = base.split(BS)[:4]
    else:
        mk, tier, ccy, doc = r['Markit_CDS_Ticker'], r['Markit_Seniority'], r['Markit_Currency'], r['Markit_Doc_Clause']
        if not all(isinstance(x, str) and x for x in (mk, tier, ccy, doc)):
            return None
    segs = _aliases().get(ticker)
    if segs and date is not None:
        hit = [m for a, b, m in segs if a <= pd.Timestamp(date) <= b]
        if hit:
            mk = hit[0]
    return BS.join([mk, tier, ccy, doc, node, str(int(coupon))])


# ---- Excel puller -----------------------------------------------------------------------------------------------------------------
class AddinSession:
    """One Excel window with the add-in XLL loaded. Use as a context manager."""

    def __enter__(self):
        import xlwings as xw
        self.app = xw.App(visible=True, add_book=True)
        self.app.api.DisplayAlerts = False
        time.sleep(4)
        self.app.api.RegisterXLL(XLL)
        time.sleep(4)
        self.ws = self.app.books[0].sheets[0]
        return self

    def __exit__(self, *exc):
        try:
            self.app.api.DisplayAlerts = False
            for b in list(self.app.books):
                b.close()
            self.app.quit()
        except Exception:
            pass

    def _run(self, ids, start, end, timeout):
        """One IHSMTimeseries call for these ids over [start, end]; returns (DataFrame or None, error text)."""
        ws = self.ws
        ws.clear()
        for i, ident in enumerate(ids, start=1):
            ws.cells(i, 1).value = ident
        n = len(ids)
        top = n + 2
        fields = ','.join(FIELDS.values())
        ws.cells(top, 3).formula = (f'=IHSMTimeseries($A$1:$A${n},"{fields}","cdspd",,DATE({start.year},{start.month},{start.day}),'
                                    f'DATE({end.year},{end.month},{end.day}),TRUE,100000)')
        t0 = time.time()
        expect = None
        while time.time() - t0 < timeout:
            time.sleep(2)
            head = ws.cells(top, 3).api.Text
            if head.startswith('#') or 'LOGIN' in head:
                return None, head
            m = re.search(r'\[(\d+)x(\d+)\]', head)
            if m:
                expect = int(m.group(1))
                last = ws.api.UsedRange.Row + ws.api.UsedRange.Rows.Count - 1
                if last >= top + expect:
                    break
        else:
            return None, f'timeout after {timeout}s (marker {expect})'
        vals = ws.range(ws.cells(top + 1, 3), ws.cells(top + expect, 2 + len(FIELDS) + 1)).value
        hdr = ['id'] + list(FIELDS.keys())
        df = pd.DataFrame(vals[1:] if isinstance(vals[0][0], str) and vals[0][0] == 'Identifier' else vals, columns=hdr)
        return df, ''

    def pull(self, ids, start, end, timeout=240, depth=0, log=print):
        """Pull ids in one call; if that fails, bisect to isolate the bad identifiers. Returns (DataFrame, {id: error})."""
        ids = list(ids)
        df, err = self._run(ids, start, end, timeout)
        if df is not None:
            return df, {}
        if len(ids) == 1:
            log(f'    {"  " * depth}failed id: {ids[0]} -> {err}')
            return pd.DataFrame(columns=['id'] + list(FIELDS)), {ids[0]: err}
        log(f'    {"  " * depth}batch of {len(ids)} failed ({err}); splitting')
        h = len(ids) // 2
        a, ea = self.pull(ids[:h], start, end, timeout, depth + 1, log)
        b, eb = self.pull(ids[h:], start, end, timeout, depth + 1, log)
        return pd.concat([a, b], ignore_index=True), {**ea, **eb}


def to_tidy(raw):
    """Raw add-in rows -> id, date, conv/par (bp), upfront (points), rpv01 (years). Blank cells stay NaN; non-numeric error strings become NaN."""
    df = raw.copy()
    df['date'] = pd.to_datetime('1899-12-30') + pd.to_timedelta(pd.to_numeric(df['asOf'], errors='coerce'), unit='D')
    for c in ('conv', 'par', 'upfront', 'rpv01'):
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df['conv'] *= 1e4
    df['par'] *= 1e4
    df['upfront'] *= 100.0
    return df[['id', 'date', 'conv', 'par', 'upfront', 'rpv01']]


def pull_batches(batches, session, log=print):
    """batches: list of (name, ids, start, end). Each is cached to cache/addin_raw/<name>.parquet and skipped on a rerun."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    errors = {}
    for k, (name, ids, start, end) in enumerate(batches, 1):
        f = RAW_DIR / f'{name}.parquet'
        if f.exists():
            log(f'[{k}/{len(batches)}] {name}: cached')
            continue
        t0 = time.time()
        df, err = session.pull(ids, start, end, log=log)
        errors.update(err)
        df.to_parquet(f, index=False)
        log(f'[{k}/{len(batches)}] {name}: {len(ids)} ids -> {len(df):,} rows, {len(err)} failed ids, {time.time() - t0:.0f}s')
    return errors


def load_raw():
    parts = [pd.read_parquet(f) for f in sorted(RAW_DIR.glob('*.parquet'))]
    return to_tidy(pd.concat(parts, ignore_index=True)) if parts else pd.DataFrame(columns=['id', 'date', 'conv', 'par', 'upfront', 'rpv01'])
