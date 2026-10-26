"""The standard way to repair a name's missing history: a review spreadsheet per name. Use the SYSTEM Python (Bloomberg, Quantifi and openpyxl are all there).

    python pricing/fix_history/review.py list                  # which names have gaps, biggest first
    python pricing/fix_history/review.py make SIRI [MDC ...]   # writes cache/review/SIRI.xlsx
    ... open it in Excel, fill the yellow `override_spread` column (copy/paste `best_guess` where you agree, type your own where you don't), save, CLOSE it ...
    python pricing/fix_history/review.py ingest SIRI           # reads the sheet, updates config/overrides.csv, prices the cells with Quantifi

Sheet `review`, one row per missing date per index: index_name | ticker | name | date | years_left | bbg_5y (Bloomberg 5Y PX_LAST) | best_guess | guess_method | override_spread |
source | note | status.  `override_spread` is the ONLY thing that counts: blank = still a gap (not approved), a number = the conv_spread in bp at the index coupon you approve.
`source` / `note` are free text recorded with each approval (they default to the guess method). Sheet `runs` lists the gaps with the neighbouring Markit prices and the fit;
sheet `charts` plots bbg_5y, best_guess and your override per index so you can eyeball it. Re-running `make` is safe: it keeps what you already entered (and what is already approved)
and archives the old file in cache/review/old/. After `ingest` the sheet is the single record for that name: ingest REPLACES all override rows for that name from it.
Cells then show as `override` (teal) in the notebooks once you run `python pricing/shared/publish.py`.
"""
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'shared'))     # find _paths (it adds the other pricing folders and analysis/)
import _paths  # noqa: F401
import numpy as np
import pandas as pd

import cdx_spreads as cs
import gaps as gp
import guess as gs
from _paths import CACHE, CONFIG

REVIEW_DIR = CACHE / 'review'
SERIES_DIR = CONFIG / 'override_series'
OVERRIDES = CONFIG / 'overrides.csv'
OV_COLS = ['index_name', 'market_cds_ticker', 'start_date', 'end_date', 'conv_spread', 'conv_spread_end', 'force', 'series', 'source', 'note']
SHEET_COLS = ['index_name', 'ticker', 'name', 'date', 'years_left', 'bbg_5y', 'best_guess', 'guess_method', 'override_spread', 'source', 'note', 'status']
COL = {c: i + 1 for i, c in enumerate(SHEET_COLS)}


def _letter(c):
    return chr(ord('A') + COL[c] - 1)


def list_gaps():
    g = gp.find_gaps(include_overrides=True)
    open_g = gp.find_gaps()
    a = g[~g['defaulted']].groupby('market_cds_ticker').agg(name=('member_name', 'first'), cells=('n_days', 'sum'), indices=('index_name', 'nunique'))
    b = open_g[~open_g['defaulted']].groupby('market_cds_ticker')['n_days'].sum().rename('still_open')
    out = a.join(b).fillna({'still_open': 0}).astype({'still_open': int}).sort_values('still_open', ascending=False)
    d = open_g[open_g['defaulted']].groupby('market_cds_ticker')['n_days'].sum()
    print(out.head(40).to_string())
    print(f'\n{len(out)} names with gaps; {int(out["still_open"].sum()):,} cells still open. Defaulted-name days without a price (not handled by review sheets): {int(d.sum()):,}')


def _existing(ticker, path):
    """Values already entered: from an existing sheet (wins), else from the currently priced overrides."""
    vals = {}
    mo = cs._load_manual()
    if mo is not None:
        for r in mo[mo['market_cds_ticker'] == ticker].itertuples():
            vals[(r.index_name, pd.Timestamp(r.published_date))] = (float(r.conv_spread), r.override_source or '', r.override_note or '')
    if path.exists():
        tmp = REVIEW_DIR / '_read.xlsx'
        shutil.copy2(path, tmp)
        old = pd.read_excel(tmp, sheet_name='review')
        tmp.unlink()
        for r in old.itertuples():
            if pd.notna(r.override_spread):
                vals[(r.index_name, pd.Timestamp(r.date))] = (float(r.override_spread), r.source if isinstance(r.source, str) else '', r.note if isinstance(r.note, str) else '')
            else:
                vals.pop((r.index_name, pd.Timestamp(r.date)), None)       # the owner cleared it
    return vals


def make(ticker):
    from openpyxl import Workbook
    from openpyxl.chart import LineChart, Reference
    from openpyxl.chart.series import SeriesLabel
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.worksheet.datavalidation import DataValidation

    REVIEW_DIR.mkdir(exist_ok=True)
    path = REVIEW_DIR / f'{ticker}.xlsx'
    runs_all = gp.find_gaps(include_overrides=True)
    runs = runs_all[(runs_all['market_cds_ticker'] == ticker) & ~runs_all['defaulted']].sort_values(['index_name', 'start_date']).reset_index(drop=True)
    if runs.empty:
        print(f'{ticker}: no gaps (or only defaulted-name days, which review sheets do not cover)')
        return
    prior = _existing(ticker, path)
    cells, info = gs.guess_cells(ticker, runs)
    cells = cells.sort_values(['index_name', 'date']).reset_index(drop=True)
    keep = [prior.get((r.index_name, r.date)) for r in cells.itertuples()]
    cells['override_spread'] = [k[0] if k else np.nan for k in keep]
    cells['source'] = [k[1] if k else '' for k in keep]
    cells['note'] = [k[2] if k else '' for k in keep]
    if path.exists():
        (REVIEW_DIR / 'old').mkdir(exist_ok=True)
        path.replace(REVIEW_DIR / 'old' / f'{ticker}_{datetime.now():%Y%m%d_%H%M%S}.xlsx')

    wb = Workbook()
    ws = wb.active
    ws.title = 'review'
    head_fill, in_fill = PatternFill('solid', fgColor='000000'), PatternFill('solid', fgColor='FFF2CC')
    for c, name in enumerate(SHEET_COLS, 1):
        cell = ws.cell(1, c, name)
        cell.font, cell.fill, cell.alignment = Font(bold=True, color='FFFFFF'), head_fill, Alignment(horizontal='center')
    for i, r in enumerate(cells.itertuples(), 2):
        vals = [r.index_name, r.market_cds_ticker, r.member_name, r.date.to_pydatetime(), r.years_left, None if pd.isna(r.bbg_5y) else float(r.bbg_5y),
                None if pd.isna(r.best_guess) else float(r.best_guess), r.guess_method, None if pd.isna(r.override_spread) else float(r.override_spread), r.source, r.note,
                f'=IF({_letter("override_spread")}{i}="","OPEN","ok")']
        for c, v in enumerate(vals, 1):
            ws.cell(i, c, v)
        ws.cell(i, COL['date']).number_format = 'yyyy-mm-dd'
        for c in ('years_left',):
            ws.cell(i, COL[c]).number_format = '0.00'
        for c in ('bbg_5y', 'best_guess', 'override_spread'):
            ws.cell(i, COL[c]).number_format = '0.0'
        for c in ('override_spread', 'source', 'note'):
            ws.cell(i, COL[c]).fill = in_fill
    n = len(cells) + 1
    for c, w in {'index_name': 11, 'ticker': 9, 'name': 26, 'date': 12, 'years_left': 10, 'bbg_5y': 9, 'best_guess': 11, 'guess_method': 15, 'override_spread': 16, 'source': 28, 'note': 40, 'status': 9}.items():
        ws.column_dimensions[_letter(c)].width = w
    ws.freeze_panes = 'A2'
    ws.auto_filter.ref = f'A1:{_letter("status")}{n}'
    dv = DataValidation(type='decimal', operator='greaterThan', formula1='0', allow_blank=True, showErrorMessage=True, errorTitle='override_spread',
                        error='conv_spread in bp must be a number above 0 (or leave blank)')
    ws.add_data_validation(dv)
    dv.add(f'{_letter("override_spread")}2:{_letter("override_spread")}{n}')

    rs = wb.create_sheet('runs')
    rcols = ['index_name', 'start_date', 'end_date', 'n_days', 'kind', 'prev_date', 'prev_conv', 'next_date', 'next_conv']
    for c, name in enumerate(rcols, 1):
        cell = rs.cell(1, c, name)
        cell.font, cell.fill = Font(bold=True, color='FFFFFF'), head_fill
        rs.column_dimensions[chr(64 + c)].width = 13
    for i, r in enumerate(runs[rcols].itertuples(index=False), 2):
        for c, v in enumerate(r, 1):
            v = None if pd.isna(v) else (v.to_pydatetime() if isinstance(v, pd.Timestamp) else v)
            rs.cell(i, c, v)
            if isinstance(v, datetime):
                rs.cell(i, c).number_format = 'yyyy-mm-dd'
    row = len(runs) + 3
    lines = [f'{ticker}: Bloomberg ticker {info["bbg_ticker"] or "none"}']
    if info['backtest']:
        k, mae, med, p90 = info['backtest']
        lines.append(f'backtest of bbg5y x ratio vs Markit on {k:,} overlap points: mean abs error {mae:.1f} bp, median {med:+.1f} bp, 90th pct abs {p90:.1f} bp (in-sample)')
    lines += info['notes']
    for ln in lines:
        rs.cell(row, 1, ln)
        row += 1
    for cpn, c in info['curves'].items():
        row += 1
        rs.cell(row, 1, f'ratio markit_conv / bbg_5y by years to maturity, {cpn} coupon indices').font = Font(bold=True)
        row += 1
        for j, h in enumerate(['years_left', 'ratio', 'n_points'], 1):
            rs.cell(row, j, h).font = Font(bold=True)
        for t, rr in c.iterrows():
            row += 1
            rs.cell(row, 1, float(t))
            rs.cell(row, 2, round(float(rr['ratio']), 3))
            rs.cell(row, 3, int(rr['n']))

    cs_ = wb.create_sheet('charts')
    pos, top = 2, 1
    for idx, g in cells.groupby('index_name', sort=True):
        r0, r1 = g.index.min() + 2, g.index.max() + 2
        ch = LineChart()
        ch.title, ch.height, ch.width = f'{idx} {ticker}: conv_spread (bp)', 8, 26
        for col in ('bbg_5y', 'best_guess', 'override_spread'):
            ch.add_data(Reference(ws, min_col=COL[col], min_row=r0, max_row=r1), titles_from_data=False)
        for ser, t in zip(ch.series, ('bbg 5Y', 'best guess', 'override')):
            ser.tx = SeriesLabel(v=t)
            ser.smooth = False
            ser.marker.symbol = 'none'
        ch.set_categories(Reference(ws, min_col=COL['date'], min_row=r0, max_row=r1))
        ch.x_axis.number_format, ch.x_axis.majorTimeUnit = 'yyyy-mm', 'months'
        ch.x_axis.delete = ch.y_axis.delete = False
        cs_.add_chart(ch, f'A{top}')
        top += 18

    rd = wb.create_sheet('readme', 0)
    for i, t in enumerate([f'{ticker}: fill the yellow override_spread column on the `review` sheet.',
                           'override_spread = the conv_spread in bp (at the index coupon, HY/XO 500, IG/MA 100) you approve for that index on that date. Blank = leave as a gap.',
                           'Copy/paste best_guess where you agree. source / note are optional text recorded with it.',
                           'Save and CLOSE the file, then run:  python pricing/fix_history/review.py ingest ' + ticker,
                           'The `runs` sheet shows the gaps with the neighbouring Markit prices; `charts` plots bbg 5Y, best guess and your override per index.'], 1):
        rd.cell(i, 1, t)
    rd.column_dimensions['A'].width = 140
    wb.active = 1
    wb.save(path)
    pre = int(cells['override_spread'].notna().sum())
    print(f'{ticker}: wrote {path} ({len(cells):,} rows, {len(runs)} runs; {pre:,} already have an override). guess methods: {cells["guess_method"].value_counts().to_dict()}')
    if info['backtest']:
        print(f'  backtest vs Markit on the overlap: mean abs error {info["backtest"][1]:.1f} bp (n={info["backtest"][0]:,})')
    for t in info['notes']:
        print('  note:', t)


def ingest(ticker, price=True):
    path = REVIEW_DIR / f'{ticker}.xlsx'
    if not path.exists():
        raise FileNotFoundError(f'{path} missing: run `make {ticker}` first')
    tmp = REVIEW_DIR / '_read.xlsx'
    shutil.copy2(path, tmp)                                      # Excel may still hold the original open
    sh = pd.read_excel(tmp, sheet_name='review')
    tmp.unlink()
    sh = sh[sh['ticker'] == ticker].copy()
    sh['date'] = pd.to_datetime(sh['date'])
    ov = pd.to_numeric(sh['override_spread'], errors='coerce')
    bad = sh[sh['override_spread'].notna() & ~(ov > 0)]
    if len(bad):
        raise ValueError(f'override_spread must be a number above 0; check rows {list(bad.index + 2)} of the review sheet')
    done = sh[ov > 0].assign(conv_spread=ov[ov > 0]).copy()
    done['source'] = done['source'].where(done['source'].notna(), '').astype(str)
    done['note'] = done['note'].where(done['note'].notna(), '').astype(str)
    done.loc[done['source'] == '', 'source'] = done['guess_method'].where(done['conv_spread'].sub(done['best_guess']).abs() < 0.005, 'manual')
    print(f'{ticker}: {len(done):,} approved cells, {len(sh) - len(done):,} left open')

    SERIES_DIR.mkdir(exist_ok=True)
    series_file = f'{ticker}_review.csv'
    old = pd.read_csv(OVERRIDES, dtype=str, encoding='utf-8-sig').fillna('') if OVERRIDES.exists() else pd.DataFrame(columns=OV_COLS)
    for c in OV_COLS:
        if c not in old.columns:
            old[c] = ''
    old_series = set(old.loc[old['market_cds_ticker'] == ticker, 'series']) - {''}
    keep = old[old['market_cds_ticker'] != ticker][OV_COLS]          # the sheet is the single record for this name
    rows = []
    for idx, g in done.groupby('index_name'):
        dates = pd.DatetimeIndex(sorted(cs.get_spread_history(idx)['published_date'].unique()))
        pos = pd.Series(np.arange(len(dates)), index=dates)
        g = g.sort_values('date').assign(pos=lambda d: d['date'].map(pos))
        run = ((g['pos'].diff() != 1) | (g['source'] != g['source'].shift()) | (g['note'] != g['note'].shift())).cumsum()
        for _, r in g.groupby(run):
            rows.append(dict(index_name=idx, market_cds_ticker=ticker, start_date=f'{r["date"].min():%Y-%m-%d}', end_date=f'{r["date"].max():%Y-%m-%d}', conv_spread='',
                             conv_spread_end='', force='', series=series_file, source=r['source'].iloc[0], note=r['note'].iloc[0]))
    new = pd.concat([keep, pd.DataFrame(rows, columns=OV_COLS)], ignore_index=True).fillna('')
    done[['index_name', 'date', 'conv_spread']].sort_values(['index_name', 'date']).to_csv(SERIES_DIR / series_file, index=False, date_format='%Y-%m-%d')
    new.to_csv(OVERRIDES, index=False, lineterminator='\r\n')
    for f in old_series - {series_file}:                          # series files of the rows just replaced
        if not (new['series'] == f).any() and (SERIES_DIR / f).exists():
            (SERIES_DIR / f).unlink()
    print(f'  wrote config/override_series/{series_file} and {len(rows)} rows in config/overrides.csv')
    if price:
        import overrides as ov_mod
        ov_mod.price_overrides()
        left = gp.find_gaps()
        left = left[left['market_cds_ticker'] == ticker]
        print(f'  {ticker}: {int(left["n_days"].sum()):,} cells still open' + (f' in {left["index_name"].nunique()} indices' if len(left) else ''))
        print('  next: python pricing/shared/publish.py (when you want the notebooks to see it)')


if __name__ == '__main__':
    cmd, *args = sys.argv[1:] or ['help']
    args = [a for a in args if not a.startswith('--')]
    if cmd == 'list':
        list_gaps()
    elif cmd == 'make' and args:
        for t in args:
            make(t)
    elif cmd == 'ingest' and args:
        for t in args:
            ingest(t, price='--no-price' not in sys.argv)
    else:
        print(__doc__)
