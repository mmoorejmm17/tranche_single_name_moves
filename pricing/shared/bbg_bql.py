"""Bloomberg BQL through the Excel add-in (blpapi has no BQL service), returned as DataFrames. System Python (needs xlwings + a running Excel with Bloomberg).

Why attach instead of launching Excel: a fresh Excel window started by a script never gets answers from the Bloomberg add-in (BDP stays '#N/A Requesting Data...');
the user's normal, already-open Excel does. So this attaches to a running instance, adds ONE scratch workbook, writes the BQL formulas, reads the spills and closes only
that workbook. Existing workbooks and app settings are never touched. Needs the Terminal logged in.

    from bbg_bql import debt_for_cds_tickers, bql
    debt_for_cds_tickers(['MPW', 'ACI'])          # keyed on market_cds_ticker (CDS_Ticker in cds_mappings_final.csv)
    bql("debt('MPT US Equity', issuedby='CAST_PARENT_SUBS')", FIELDS)
CLI: python pricing/shared/bbg_bql.py MPW ACI
"""
import sys
import time
import pandas as pd

MAPPINGS = 'S:/Structured Credit/Matt Stuff/cds_mappings/cds_mappings_final.csv'
# equity ticker used for debt(): the ultimate parent first (issuedby='CAST_PARENT_SUBS' then walks parent + subsidiaries), else the entity's own BBG_Equity_Ticker, else the public-equity entity.
# Open question (user, 2026-11-03): BBG_Equity_Ticker first instead? It differs from the parent for ~190 of 514 names and is often a placeholder id (MPW: 0750910D US Equity vs MPT US Equity).
EQUITY_COLS = ['Ultimate_Parent_Equity_Ticker', 'BBG_Equity_Ticker', 'CDS_Public_Equity_Entity_Ticker']
FIELDS = "id, name, srch_asset_class, crncy, issue_dt, maturity, amt_outstanding(currency=USD), amt_outstanding, cpn"
ISSUEDBY = 'CAST_PARENT_SUBS'


def equity_ticker(market_cds_ticker, mappings=MAPPINGS):
    """market_cds_ticker -> Bloomberg equity ticker ('MPT US Equity'); None if the name has no public equity in the mappings file."""
    m = pd.read_csv(mappings, dtype=str, encoding='cp1252').drop_duplicates('CDS_Ticker').set_index('CDS_Ticker')
    if market_cds_ticker not in m.index:
        return None
    r = m.loc[market_cds_ticker]
    for c in EQUITY_COLS:
        v = r.get(c)
        if isinstance(v, str) and v.strip() and v.strip() not in ('--', 'NaN'):
            return v.strip()
    return None


def _to_date(v):
    """Excel serial number or datetime -> Timestamp (NaT for blank / text)."""
    if isinstance(v, (int, float)) and not pd.isna(v):
        return pd.Timestamp('1899-12-30') + pd.Timedelta(days=float(v))
    return pd.to_datetime(v, errors='coerce')


def _find_app(pid=None):
    """A running Excel with the Bloomberg COM add-in connected (a busy / modal instance is skipped). pid forces one."""
    import xlwings as xw
    for app in xw.apps:
        if pid is not None and app.pid != pid:
            continue
        try:
            if any('Bloomberg' in a.Description and a.Connect for a in app.api.COMAddIns):
                _ = app.books.count
                return app
        except Exception:
            continue
    raise RuntimeError('no running Excel with the Bloomberg add-in connected (open Excel, log in to the Terminal)' + (f' [pid {pid}]' if pid else ''))


def bql_many(queries, fields=FIELDS, timeout=180, pid=None, log=print):
    """queries: {key: bql_expression} (the first argument of =BQL(), e.g. "debt('MPT US Equity', issuedby='CAST_PARENT_SUBS')").
    All run in ONE scratch workbook, side by side (one block of columns each, so spills never collide). Returns ({key: DataFrame}, {key: error text})."""
    app = _find_app(pid)
    log(f'attaching to Excel pid {app.pid}')
    wb = app.books.add()
    out, err = {}, {}
    try:
        ws = wb.sheets[0]
        width = len(fields.split(',')) + 3
        cells = {}
        for i, (key, q) in enumerate(queries.items()):
            cell = ws.cells(1, 1 + i * width)
            cell.formula2 = '=BQL("' + q.replace('"', '""') + '","' + fields.replace('"', '""') + '")'   # formula2: dynamic-array spill
            cells[key] = cell
        if app.api.Calculation != -4105:                       # xlCalculationAutomatic; if the user's Excel is manual, recalc this sheet only
            ws.api.Calculate()
        t0 = time.time()
        while time.time() - t0 < timeout:
            time.sleep(2)
            if not any('Requesting' in (c.api.Text or '') for c in cells.values()):
                break
        else:
            log(f'timeout after {timeout}s; reading what has arrived')
        time.sleep(1)
        for key, cell in cells.items():
            head = cell.api.Text or ''
            if head.startswith('#') or 'Requesting' in head:
                err[key] = head or 'empty'
                continue
            try:
                rng = cell.api.SpillingToRange
                df = ws.range(rng.Address).options(pd.DataFrame, header=1, index=False).value
            except Exception:
                df = cell.expand().options(pd.DataFrame, header=1, index=False).value
            if 'ID' in df.columns and 'id' in df.columns:     # BQL adds its own ID column, duplicating the requested id
                df = df.drop(columns='ID')
            for c in df.columns:                              # date columns arrive as real dates or as Excel serials depending on the cell format: make them all dates
                if c.endswith('_dt') or c in ('maturity', 'asof_date', 'date'):
                    df[c] = df[c].map(_to_date)
            out[key] = df
    finally:
        try:
            wb.api.Saved = True                                # scratch workbook: close without the save prompt
            wb.close()
        except Exception:
            pass
    return out, err


def bql(expression, fields=FIELDS, **kw):
    """One BQL expression -> DataFrame (raises with Excel's error text if it fails)."""
    out, err = bql_many({'q': expression}, fields, **kw)
    if 'q' in err:
        raise RuntimeError(f'BQL failed: {err["q"]}')
    return out['q']


def debt_for_cds_tickers(tickers, fields=FIELDS, issuedby=ISSUEDBY, mappings=MAPPINGS, **kw):
    """Debt of each name, keyed on market_cds_ticker: one DataFrame with a market_cds_ticker and equity_ticker column added (names with no equity ticker, or a failed
    query, are reported and skipped)."""
    if isinstance(tickers, str):
        tickers = [tickers]
    qs, eq = {}, {}
    for t in tickers:
        e = equity_ticker(t, mappings)
        if e is None:
            print(f'{t}: no equity ticker in the mappings file (private / unmapped), skipped')
            continue
        eq[t] = e
        qs[t] = f"debt('{e}', issuedby='{issuedby}')"
    if not qs:
        return pd.DataFrame()
    out, err = bql_many(qs, fields, **kw)
    for t, msg in err.items():
        print(f'{t} ({eq[t]}): BQL returned {msg}')
    parts = [d.assign(market_cds_ticker=t, equity_ticker=eq[t]) for t, d in out.items()]
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


if __name__ == '__main__':
    df = debt_for_cds_tickers(sys.argv[1:] or ['MPW'])
    pd.set_option('display.width', 250, 'display.max_columns', 30)
    sys.stdout.reconfigure(encoding='utf-8')
    print(df.to_string())
