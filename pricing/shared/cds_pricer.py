"""Re-price a single-name CDS curve point at any coupon, using the Quantifi Toolkit.

A curve point is (name, as-of date, maturity). It is PRICED if we have a par_spread, or a conv_spread / upfront together with the coupon that quote is at
(risky_pv01 is a bonus: when it is missing it is derived from a flat survival curve, a few percent off Markit's). The curve is assumed flat across tenors.

Everything reduces to a canonical pair (par_spread [bp], risky_pv01 [years]); from it, for ANY coupon C (bp):

    all_upfront = par_spread x risky_pv01 / 100                 (points; coupon-free)
    upfront(C)  = (par_spread - C) x risky_pv01 / 100           (points)
    conv_spread(C) = flat spread whose upfront at coupon C equals upfront(C)   (what you plug into CDSW; depends on C)

so switching coupon is arithmetic, and conv_spread comes from one Toolkit conversion. Resolution order for the canonical pair (route):
    par+rpv01         par_spread and risky_pv01 given                        -> used as is
    upfront+rpv01      upfront, coupon, risky_pv01                            -> par = C + upfront x 100 / risky_pv01
    conv+rpv01         conv_spread (> 0), coupon, risky_pv01                  -> conv -> upfront with a flat curve, then par as above
    par (rpv01 derived)           par_spread only                              -> risky_pv01 from a flat curve fitted to the par spread
    upfront (rpv01 derived)       upfront + coupon only                        -> par and risky_pv01 from a flat curve fitted to the upfront
    conv (rpv01 derived)          conv_spread + coupon only                    -> par and risky_pv01 from a flat curve fitted to the conv spread
Assumptions (printed with every result): standard recovery 40%, flat continuously compounded 4% discount curve, SNAC (STEC for EUR) conventions,
one node at the maturity. If the all-in upfront is too large for the standard recovery (wide names), conv_spread is converted at a marked-down
recovery (100 - all_upfront - 5 points, see cds_recalib.py) and flagged 'recal'.

The Toolkit is installed in the system Python, not the project .venv, so run this with the system Python. `curve()` returns a Quantifi SurvivalCurve
you can pass to other Toolkit functions. price_rows() does a DataFrame of points at once.
"""
import math
from pathlib import Path

import pandas as pd

import cds_recalib as cr

RECOVERY = 0.40
RATE = cr.RATE
CONTINUOUS_MARKDOWN = True      # recovery = min(40%, 100 - all_upfront - 5) for every point; False = old rule (40% unless the fit fails)


def _has(x):
    return x is not None and not pd.isna(x)


def is_priced(par_spread=None, risky_pv01=None, upfront=None, conv_spread=None, coupon=None):
    """User's definition: a par_spread (risky_pv01 optional, derivable), or a conv_spread (> 0, 0 is Markit's placeholder) / upfront with its coupon."""
    return _has(par_spread) or (_has(coupon) and (_has(upfront) or (_has(conv_spread) and conv_spread > 0)))


class CdsCurvePricer:
    def __init__(self, asof, maturity, ccy='USD', recovery=RECOVERY, rate=RATE, par_spread=None, risky_pv01=None, upfront=None,
                 conv_spread=None, coupon=None, _tk=None, _curves=None, all_upfront=None):
        self.asof, self.maturity, self.ccy = pd.Timestamp(asof), pd.Timestamp(maturity), ccy
        self.recovery, self.rate = recovery, rate
        self._tk = _tk or cr._toolkit()
        self._curves = _curves if _curves is not None else {}
        self.coupon = coupon
        self.par = self.rpv = None
        self.route = ''
        self.rpv01_source = 'data'
        self._resolve(par_spread, risky_pv01, upfront, conv_spread, all_upfront)

    # ---- Toolkit helpers -------------------------------------------------------------------------------------------------------
    def _fit(self, qtype, quote, coupon_bp, recovery):
        tk = self._tk
        key = (self.asof, self.rate)
        if key not in self._curves:
            self._curves[key] = cr._flat_curve(tk, self.asof, self.rate)
        dc = self._curves[key]
        terms = tk['CdsTerms'].Get('STEC' if self.ccy == 'EUR' else 'SNAC', self.ccy)
        params = tk['SurvivalCurveParameters'](terms.DayCount, terms.Frequency, terms.BDConvention, terms.PremCalendar)
        a, m = cr._dt(tk, self.asof), cr._dt(tk, self.maturity)
        return tk['SurvivalCurve'].FitCDSQuotes(
            name='pricer', asOf=a, settle=tk['Dt'].AddDays(a, 1, None), ccy=self.ccy, category='Corp', isStandardContract=True,
            cdsQuoteType=qtype, runningPremium=float(coupon_bp), parameters=params, discountCurve=dc, tenorNames=['5Y'], tenorDates=[m],
            quotes=[quote], recoveries=[recovery], recoveryDisp=0.0, eventDates=[None, None, None], refinanceCurve=None, corr=0.0,
            convRecoveryRate=recovery, convDiscountCurve=dc, forcefit=False)

    def _par_rpv(self, curve):
        m = cr._dt(self._tk, self.maturity)
        return self._tk['CurveUtil'].ImpliedSpread(curve, m) * 1e4, self._tk['CurveUtil'].ImpliedDuration(curve, m)

    # ---- canonical (par_spread, risky_pv01) ------------------------------------------------------------------------------------
    def _resolve(self, par, rpv, up, conv, allup=None):
        qt = self._tk['CDSQuoteType']
        c = self.coupon
        conv = conv if (_has(conv) and conv > 0) else None
        # (tested: refining risky_pv01 from all_upfront / par helps wide names but hurts tight ones by up to 0.3 pts, so risky_pv01 is used as stored)
        if _has(par) and _has(rpv):
            self.par, self.rpv, self.route = float(par), float(rpv), 'par+rpv01'
        elif _has(up) and _has(c) and _has(rpv):
            self.par, self.rpv, self.route = c + up * 100.0 / rpv, float(rpv), 'upfront+rpv01'
        elif conv is not None and _has(c) and _has(rpv):
            par_m, rpv_m = self._par_rpv(self._fit(qt.ConvSpread, conv, c, self.recovery))
            up_m = (par_m - c) * rpv_m / 100.0
            self.par, self.rpv, self.route = c + up_m * 100.0 / rpv, float(rpv), 'conv+rpv01'
        elif _has(par):
            self.par = float(par)
            self.rpv = self._par_rpv(self._fit(qt.ParSpread, float(par), 0.0, self.recovery))[1]
            self.route, self.rpv01_source = 'par (rpv01 derived)', 'derived'
        elif _has(up) and _has(c):
            self.par, self.rpv = self._par_rpv(self._fit(qt.Upfront, up / 100.0, c, self.recovery))
            self.route, self.rpv01_source = 'upfront (rpv01 derived)', 'derived'
        elif conv is not None and _has(c):
            self.par, self.rpv = self._par_rpv(self._fit(qt.ConvSpread, conv, c, self.recovery))
            self.route, self.rpv01_source = 'conv (rpv01 derived)', 'derived'
        else:
            raise ValueError('not priced: need a par_spread, or an upfront / conv_spread (> 0) with its coupon')

    @property
    def par_spread(self):
        return self.par

    @property
    def risky_pv01(self):
        return self.rpv

    # ---- re-price at any coupon ------------------------------------------------------------------------------------------------
    def _conv(self, upfront_pts, coupon, all_upfront):
        """conv_spread at `coupon` for that upfront. -> (conv_bp, status, recovery).

        Recovery is min(standard 40%, (100 - all_upfront - 5) / 100): the standard 40% for every normal name, marked down smoothly once all_upfront passes 55 (so the
        spread never jumps between a 40% basis and a marked-down one; near the edge of what 40% allows the standard conv_spread explodes, e.g. 19,000 bp, and then stops
        solving, which made moves across that edge meaningless). Status 'ok' = standard recovery, 'recal' = marked down. If even that fails the Toolkit raises
        SurvivalFitException and the point gets a NaN conv_spread, reported as 'failed'."""
        rec_mark = cr.recovery_for(all_upfront)[0]
        rec = min(self.recovery, rec_mark) if CONTINUOUS_MARKDOWN else self.recovery
        try:
            return (cr.fit_row(self._tk, self.asof, self.maturity, coupon, upfront_pts, rec, self.ccy, self.rate, self._curves)[0],
                    'ok' if rec >= self.recovery else 'recal', rec)
        except Exception:
            pass
        rec, floored = cr.recovery_for(all_upfront)
        try:
            conv = cr.fit_row(self._tk, self.asof, self.maturity, coupon, upfront_pts, rec, self.ccy, self.rate, self._curves)[0]
            return conv, 'recal', rec
        except Exception as e:
            return float('nan'), f'failed: {type(e).__name__}', rec

    def at_coupon(self, coupon):
        """All the stats at a running coupon (bp): dict(coupon, par_spread, risky_pv01, upfront, all_upfront, conv_spread, conv_status, recovery_used %, ...)."""
        up = (self.par - coupon) * self.rpv / 100.0
        allup = self.par * self.rpv / 100.0
        conv, status, rec = self._conv(up, coupon, allup)
        return dict(coupon=float(coupon), par_spread=self.par, risky_pv01=self.rpv, upfront=up, all_upfront=allup, conv_spread=conv,
                    conv_status=status, recovery_used=rec * 100.0, route=self.route, rpv01_source=self.rpv01_source)

    def curve(self, coupon=None):
        """A calibrated Quantifi SurvivalCurve (flat, one node at the maturity) reproducing this point's upfront at `coupon` (default: its own, else 500)."""
        c = coupon if coupon is not None else (self.coupon if _has(self.coupon) else 500.0)
        up = (self.par - c) * self.rpv / 100.0
        rec = self.recovery
        if CONTINUOUS_MARKDOWN:      # same recovery rule as at_coupon()
            rec = min(rec, cr.recovery_for(self.par * self.rpv / 100.0)[0])
        try:
            return self._fit(self._tk['CDSQuoteType'].Upfront, up / 100.0, c, rec)
        except Exception:   # no solution even so: fall back to the marked-down recovery
            return self._fit(self._tk['CDSQuoteType'].Upfront, up / 100.0, c, cr.recovery_for(self.par * self.rpv / 100.0)[0])

    def assumptions(self):
        return (f'recovery {self.recovery:.0%} (marked down for wide names), flat {self.rate:.2%} discount, '
                f'{"STEC" if self.ccy == "EUR" else "SNAC"} terms, curve flat across tenors; route: {self.route}')


def price_rows(rows, target_coupon, recovery=RECOVERY, rate=RATE):
    """Re-price a DataFrame of curve points at `target_coupon` (a number, or a Series/array aligned to `rows`, e.g. each row's index coupon).

    rows needs published_date, cds_maturity and whichever of par_spread, risky_pv01, upfront, conv_spread, running_coupon, currency it has.
    Returns one row per input with the at_coupon() stats; unpriced rows give route='not priced' and NaNs.
    """
    tk, curves = cr._toolkit(), {}
    tc = pd.Series(target_coupon, index=rows.index) if not hasattr(target_coupon, '__len__') else pd.Series(list(target_coupon), index=rows.index)
    out = []
    for (i, r), c in zip(rows.iterrows(), tc):
        g = lambda k: r[k] if k in rows.columns else None
        try:
            p = CdsCurvePricer(r['published_date'], r['cds_maturity'], g('currency') or 'USD', recovery, rate, g('par_spread'), g('risky_pv01'),
                               g('upfront'), g('conv_spread'), g('running_coupon'), tk, curves, g('all_upfront'))
            out.append(p.at_coupon(float(c)))
        except Exception as e:
            out.append(dict(coupon=float(c), route='not priced' if isinstance(e, ValueError) else f'failed: {type(e).__name__}: {str(e)[:60]}'))
    return pd.DataFrame(out, index=rows.index)


STORE_FILE = cr.STORE_FILE
OUT_FILE = cr.STORE_DIR / 'prices' / 'priced_at_index_coupon.parquet'
POINT_KEY = ['market_cds_ticker', 'published_date', 'cds_maturity', 'running_coupon', 'index_coupon']


def price_store(index_names=None, out_file=OUT_FILE):
    """Re-price every priced point in the store at its index's own coupon (500 HY/XO, 100 IG/MA) and write cache/priced_at_index_coupon.parquet.

    Points are unique on (ticker, date, maturity, quoted coupon, index coupon), so indices that share a maturity are priced once. Output columns:
    the key plus the at_coupon() stats (par_spread, risky_pv01, upfront, all_upfront, conv_spread, conv_status, recovery_used, route, rpv01_source).
    """
    cols = ['index_name', 'index_coupon', 'market_cds_ticker', 'published_date', 'cds_maturity', 'currency', 'has_data', 'running_coupon',
            'par_spread', 'risky_pv01', 'upfront', 'conv_spread']
    st = pd.read_parquet(STORE_FILE, columns=cols)
    if index_names is not None:
        st = st[st['index_name'].isin(list(index_names))]
    st = st[st['has_data']]
    import time
    parts_dir = cr.WORK / 'priced_parts'      # one file per index, so an interrupted run resumes where it stopped
    parts_dir.mkdir(parents=True, exist_ok=True)
    names = list(dict.fromkeys(st['index_name']))
    print(f'{len(st):,} stored rows in {len(names)} indices; re-pricing at each index coupon (parts in {parts_dir})', flush=True)
    t0 = time.time()
    for k, name in enumerate(names, 1):
        part = parts_dir / f'{name}.parquet'
        if part.exists():
            print(f'[{k}/{len(names)}] {name}: already done, skipping', flush=True)
            continue
        pts = st[st['index_name'] == name].drop_duplicates(POINT_KEY).reset_index(drop=True)
        pts = pts[[is_priced(r.par_spread, r.risky_pv01, r.upfront, r.conv_spread, r.running_coupon) for r in pts.itertuples()]]
        o = price_rows(pts, pts['index_coupon'].values)
        res = pd.concat([pts[POINT_KEY].reset_index(drop=True), o.drop(columns='coupon').reset_index(drop=True)], axis=1)
        res.to_parquet(part, index=False)
        print(f'[{k}/{len(names)}] {name}: {len(res):,} points | conv_status {res.conv_status.value_counts().to_dict()} | {(time.time() - t0) / 60:.1f} min elapsed', flush=True)
    res = pd.concat([pd.read_parquet(parts_dir / f'{n}.parquet') for n in names], ignore_index=True).drop_duplicates(POINT_KEY)
    res.to_parquet(out_file, index=False)
    print(f'wrote {out_file}: {len(res):,} points | conv_status {res.conv_status.value_counts().to_dict()} | route {res.route.value_counts().to_dict()}', flush=True)
    return res





def reprice_wide(threshold=50.0, out_file=OUT_FILE):
    """Re-price, in place, only the points of cache/priced_at_index_coupon.parquet whose all_upfront exceeds `threshold` (below 55 the recovery rule changes
    nothing, so a threshold under 55 is just a margin). Used after changing the recovery rule, to avoid re-running the whole store."""
    p = pd.read_parquet(out_file)
    st = pd.read_parquet(STORE_FILE, columns=['market_cds_ticker', 'published_date', 'cds_maturity', 'running_coupon', 'currency', 'has_data'])
    ccy = st[st['has_data']].drop_duplicates(['market_cds_ticker', 'published_date', 'cds_maturity', 'running_coupon'])
    sel = p['all_upfront'] > threshold
    pts = p.loc[sel, POINT_KEY + ['par_spread', 'risky_pv01']].merge(
        ccy[['market_cds_ticker', 'published_date', 'cds_maturity', 'running_coupon', 'currency']], on=['market_cds_ticker', 'published_date', 'cds_maturity', 'running_coupon'], how='left')
    pts['currency'] = pts['currency'].fillna('USD')
    print(f'{len(pts):,} points with all_upfront > {threshold:g} of {len(p):,}; old conv_status {p.loc[sel, "conv_status"].value_counts().to_dict()}', flush=True)
    o = price_rows(pts, pts['index_coupon'].values)
    cols = ['conv_spread', 'conv_status', 'recovery_used', 'upfront', 'all_upfront']
    new = pts[POINT_KEY].reset_index(drop=True).join(o.reset_index(drop=True)[cols])
    keep = p.loc[~sel]
    changed = p.loc[sel, POINT_KEY + ['conv_spread']].merge(new[POINT_KEY + ['conv_spread']], on=POINT_KEY, suffixes=('_old', '_new'))
    print(f'conv_spread changed on {(changed.conv_spread_old.sub(changed.conv_spread_new).abs() > 0.5).sum():,} points; new conv_status {new.conv_status.value_counts().to_dict()}', flush=True)
    base = p.loc[sel].drop(columns=cols).merge(new, on=POINT_KEY, how='left')
    res = pd.concat([keep, base[p.columns]], ignore_index=True).sort_values(POINT_KEY).reset_index(drop=True)
    res.to_parquet(out_file, index=False)
    print(f'wrote {out_file}: {len(res):,} points', flush=True)
    return res


if __name__ == '__main__':
    import sys
    reprice_wide() if sys.argv[1:] == ['rewide'] else price_store()
