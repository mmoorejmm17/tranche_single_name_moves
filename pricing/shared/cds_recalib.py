"""Recalibrate single-name CDS curves for rows Markit gives no conventional spread (conv_spread = 0, very wide names).

Markit converts upfront -> conv_spread at a fixed 40% recovery; when the all-in upfront exceeds 100 x (1 - 40%) no spread solves
and it publishes 0. Here we instead mark the recovery down so a solution exists, using the rule agreed with the user:

    recovery (%) = 100 - all_upfront (points) - RECOVERY_CUSHION        (floored at MIN_RECOVERY)

then fit a survival curve to the upfront at the row's coupon, with a flat RATE discount curve, using the Quantifi Toolkit. The curve's
par spread at the row's maturity is the replacement conv_spread and its risky duration the replacement risky_pv01. The recovery mark is
wrong by construction (it is chosen to make the fit solvable); the point is a clean, comparable spread, not a true recovery.

Needs the Quantifi Toolkit, which is installed in the system Python but not in the project .venv, so run this module with the system
Python (it writes cache/recalibrated.parquet; cdx_spreads.spread_changes then reads that file and needs no Toolkit):

    python pricing/shared/cds_recalib.py            # recalibrate every conv_spread = 0 row in the store
"""
import math
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]      # project root (this file lives in pricing/shared/)
WORK = ROOT / 'cache' / 'work'                          # scratch (raw add-in pulls, resumable parts, backups)
STORE_DIR = ROOT / 'cache' / 'base'                     # the working copy of the store (published into data/base by pricing/shared/publish.py)
STORE_FILE = STORE_DIR / 'prices' / 'spreads.parquet'
OUT_FILE = STORE_DIR / 'prices' / 'recalibrated.parquet'
RATE = 0.04               # flat continuously compounded discount rate (user's assumption when no curve is available)
RECOVERY_CUSHION = 5.0    # points of recovery left below 100 - all_upfront
MIN_RECOVERY = 0.0        # recovery floor (%); if 100 - all_upfront - cushion is below this, it is clamped and flagged
KEY = ['market_cds_ticker', 'published_date', 'cds_maturity', 'running_coupon']
_YEARS = (0.25, 0.5, 1, 2, 3, 5, 7, 10, 15, 20, 30)


def _toolkit():
    from Quantifi.Toolkit.Base import CDSQuoteType, Dt
    from Quantifi.Toolkit.Curves import CurveUtil, DiscountCurve, SurvivalCurve, SurvivalCurveParameters
    from Quantifi.Toolkit.Numerics import Weighted
    from Quantifi.Toolkit.Products.StandardProductTerms import CdsTerms
    return dict(CDSQuoteType=CDSQuoteType, Dt=Dt, CurveUtil=CurveUtil, DiscountCurve=DiscountCurve, SurvivalCurve=SurvivalCurve,
                SurvivalCurveParameters=SurvivalCurveParameters, Weighted=Weighted, CdsTerms=CdsTerms)


def _dt(tk, ts):
    return tk['Dt'](ts.day, ts.month, ts.year)


def _flat_curve(tk, asof, rate):
    dc = tk['DiscountCurve'](_dt(tk, asof))
    dc.Name = f'flat{rate:.4f}'
    dc.Interp = tk['Weighted']()
    for yrs in _YEARS:
        d = asof + pd.DateOffset(months=round(12 * yrs))
        dc.Add(_dt(tk, d), math.exp(-rate * (d - asof).days / 365.0))
    return dc


def recovery_for(all_upfront):
    """Recovery (fraction) = (100 - all_upfront - cushion) / 100, floored; second value says whether the floor applied."""
    raw = 100.0 - all_upfront - RECOVERY_CUSHION
    return max(raw, MIN_RECOVERY) / 100.0, raw < MIN_RECOVERY


def fit_row(tk, asof, maturity, coupon_bp, upfront_pts, recovery, ccy, rate=RATE, curves=None):
    """Fit a one-node survival curve to an upfront at a given recovery; returns (conv_spread_bp, risky_pv01)."""
    cache = curves if curves is not None else {}
    key = (asof, rate)
    if key not in cache:
        cache[key] = _flat_curve(tk, asof, rate)
    dc = cache[key]
    terms = tk['CdsTerms'].Get('STEC' if ccy == 'EUR' else 'SNAC', ccy)
    params = tk['SurvivalCurveParameters'](terms.DayCount, terms.Frequency, terms.BDConvention, terms.PremCalendar)
    a, m = _dt(tk, asof), _dt(tk, maturity)
    curve = tk['SurvivalCurve'].FitCDSQuotes(
        name='recal', asOf=a, settle=tk['Dt'].AddDays(a, 1, None), ccy=ccy, category='Corp', isStandardContract=True,
        cdsQuoteType=tk['CDSQuoteType'].Upfront, runningPremium=float(coupon_bp), parameters=params, discountCurve=dc,
        tenorNames=['5Y'], tenorDates=[m], quotes=[upfront_pts / 100.0], recoveries=[recovery], recoveryDisp=0.0,
        eventDates=[None, None, None], refinanceCurve=None, corr=0.0, convRecoveryRate=recovery, convDiscountCurve=dc,
        forcefit=False)
    return tk['CurveUtil'].ImpliedSpread(curve, m) * 1e4, tk['CurveUtil'].ImpliedDuration(curve, m)


def recalibrate(rows, rate=RATE, recovery_override=None):
    """rows: DataFrame with KEY columns + upfront, all_upfront, currency.

    Adds conv_spread_recal [bp], risky_pv01_recal, recovery_used [%], recal_status. recovery_override (a fraction) forces one recovery
    for every row; it is only for validating the method on normal names at 40%.
    """
    tk = _toolkit()
    curves, res = {}, []
    for r in rows.itertuples(index=False):
        if recovery_override is None:
            recovery, floored = recovery_for(r.all_upfront)
        else:
            recovery, floored = recovery_override, False
        out = dict(conv_spread_recal=float('nan'), risky_pv01_recal=float('nan'), recovery_used=recovery * 100, recal_status='')
        try:
            out['conv_spread_recal'], out['risky_pv01_recal'] = fit_row(
                tk, r.published_date, r.cds_maturity, r.running_coupon, r.upfront, recovery, r.currency, rate, curves)
            out['recal_status'] = 'ok (recovery floored)' if floored else 'ok'
        except Exception as e:                      # a failed fit stays NaN and is reported, never silently filled
            out['recal_status'] = f'failed: {type(e).__name__}: {str(e)[:80]}'
        res.append(out)
    return pd.concat([rows[KEY].reset_index(drop=True), pd.DataFrame(res)], axis=1)


def wide_rows(store):
    """Rows needing recalibration: Databricks has a row but conv_spread is 0, and the upfronts we need are present."""
    m = store['has_data'] & (store['conv_spread'] == 0) & store['upfront'].notna() & store['all_upfront'].notna()
    return store.loc[m, KEY + ['upfront', 'all_upfront', 'currency']].drop_duplicates(KEY)


def main():
    store = pd.read_parquet(STORE_FILE)
    rows = wide_rows(store)
    print(f'{len(rows):,} conv_spread = 0 rows across {rows.market_cds_ticker.nunique()} names; flat rate {RATE:.2%}, '
          f'recovery = 100 - all_upfront - {RECOVERY_CUSHION:g} (floor {MIN_RECOVERY:g}%)')
    out = recalibrate(rows)
    OUT_FILE.parent.mkdir(exist_ok=True)
    out.to_parquet(OUT_FILE, index=False)
    ok = out['recal_status'].str.startswith('ok')
    print(f'done: {ok.sum():,} fitted, {(~ok).sum():,} failed -> {OUT_FILE}')
    print(out.loc[~ok, 'recal_status'].value_counts().head(5).to_string())


if __name__ == '__main__':
    main()
