"""Small dependency-free SVG charts for the notebooks (the project .venv has no plotting library).

line_chart(...) returns an HTML string (show it with IPython.display.HTML). Built to the dataviz method: 2px lines, hairline solid grid, categorical hues in fixed slot order
(validated palette, see PALETTE below), a legend for every chart with direct end labels, a 2px surface ring on end dots, a crosshair + one tooltip listing every series (pure
CSS, no JavaScript, so it works wherever notebook HTML renders), and a table view behind every chart. Optional shaded bands (e.g. dates with sparse pricing) and event markers
(e.g. credit events). Values never rely on the tooltip: they are also in the table view.
"""
import html
import math
import uuid

import numpy as np
import pandas as pd

# categorical slots 1-4, light / dark (palette.md; validated: adjacent CVD dE >= 8.4, normal-vision >= 19.8; aqua and yellow are < 3:1 on the light surface, so labels + table view are mandatory)
SERIES_LIGHT = ['#2a78d6', '#eb6834', '#1baf7a', '#eda100']
SERIES_DARK = ['#3987e5', '#d95926', '#199e70', '#c98500']
LIGHT = dict(surface='#fcfcfb', ink='#0b0b0b', ink2='#52514e', muted='#898781', grid='#e1e0d9', axis='#c3c2b7', border='rgba(11,11,11,0.10)')
DARK = dict(surface='#1a1a19', ink='#f0efec', ink2='#c3c2b7', muted='#898781', grid='#2c2c2a', axis='#383835', border='rgba(255,255,255,0.10)')
FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'
MAX_ZONES = 500          # hover zones per chart (a zone is one date; more dates than this are thinned evenly)


def _esc(x):
    return html.escape(str(x), quote=True)


def _nice_step(raw):
    exp = 10 ** math.floor(math.log10(raw))
    f = raw / exp
    return (1 if f <= 1 else 2 if f <= 2 else 5 if f <= 5 else 10) * exp


def y_ticks(lo, hi, n=5):
    step = _nice_step((hi - lo) / n if hi > lo else 1.0)
    start = math.floor(lo / step) * step
    ticks, v = [], start
    while v <= hi + step * 0.999:
        ticks.append(round(v, 10))
        v += step
        if len(ticks) > 12:
            break
    return [t for t in ticks if t >= lo - 1e-9][:n + 2]


def time_ticks(d0, d1, max_ticks=8):
    """Month-aligned ticks: the smallest step (1, 3, 6, 12, 24 months) that gives <= max_ticks labels."""
    for step in (1, 3, 6, 12, 24, 60):
        ticks, d = [], pd.Timestamp(d0.year, ((d0.month - 1) // step) * step + 1, 1)
        while d <= d1:
            if d >= d0:
                ticks.append(d)
            d = d + pd.DateOffset(months=step)
        if len(ticks) <= max_ticks:
            return ticks, step
    return ticks, step


def _fmt_default(v):
    return '-' if v is None or (isinstance(v, float) and np.isnan(v)) else f'{v:,.1f}'


def line_chart(dates, series, title, subtitle='', y_label='', fmt=_fmt_default, bands=None, band_label='', events=None, event_label='', tip_extra=None,
               table_extra=None, height=330, y_min=0.0, y_max=None, label_peak=True):
    """dates: sequence of timestamps. series: list of dict(name, values, [short], [slot], [muted]); short = the label drawn at the line end (defaults to name, keep it short); muted=True draws a gray context line (not a categorical slot).

    bands: list of (start, end) timestamps shaded as a wash (legend entry band_label). events: list of (date, text) drawn as small markers on the x axis (legend entry
    event_label). tip_extra: optional list of strings (one per date) appended to the tooltip. table_extra: optional dict(column -> list of strings) added to the table view.
    """
    cid = 'viz' + uuid.uuid4().hex[:8]
    dates = pd.to_datetime(pd.Series(list(dates))).reset_index(drop=True)
    n = len(dates)
    W, H = 920, height
    ml, mr, mt, mb = 56, 128, 16, 34
    pw, ph = W - ml - mr, H - mt - mb
    vals = [np.array(s['values'], dtype=float) for s in series]
    allv = np.concatenate([v[~np.isnan(v)] for v in vals]) if any((~np.isnan(v)).any() for v in vals) else np.array([0.0, 1.0])
    lo = y_min if y_min is not None else float(allv.min())
    hi = y_max if y_max is not None else float(allv.max()) * 1.08
    if hi <= lo:
        hi = lo + 1.0
    yt = y_ticks(lo, hi)
    hi = max(hi, yt[-1]) if y_max is None else hi
    x0, x1 = dates.iloc[0], dates.iloc[-1]
    span = max((x1 - x0).days, 1)
    X = lambda d: ml + pw * (pd.Timestamp(d) - x0).days / span
    Y = lambda v: mt + ph * (1 - (v - lo) / (hi - lo))
    colors = []
    k = 0
    for s in series:
        if s.get('muted'):
            colors.append('var(--muted)')
        else:
            colors.append(f'var(--s{s.get("slot", k) + 1})')
            k += 1

    out = []
    css = f"""
#{cid} {{ --surface:{LIGHT['surface']}; --ink:{LIGHT['ink']}; --ink2:{LIGHT['ink2']}; --muted:{LIGHT['muted']}; --grid:{LIGHT['grid']}; --axis:{LIGHT['axis']}; --border:{LIGHT['border']};
  {' '.join(f'--s{i + 1}:{c};' for i, c in enumerate(SERIES_LIGHT))} color-scheme: light; font-family:{FONT}; background:var(--surface); color:var(--ink); border:1px solid var(--border);
  border-radius:10px; padding:14px 16px 10px; margin:6px 0; max-width:960px; }}
@media (prefers-color-scheme: dark) {{ #{cid} {{ --surface:{DARK['surface']}; --ink:{DARK['ink']}; --ink2:{DARK['ink2']}; --muted:{DARK['muted']}; --grid:{DARK['grid']}; --axis:{DARK['axis']};
  --border:{DARK['border']}; {' '.join(f'--s{i + 1}:{c};' for i, c in enumerate(SERIES_DARK))} color-scheme: dark; }} }}
#{cid} h3 {{ margin:0 0 2px; font-size:15px; font-weight:600; color:var(--ink); }}
#{cid} .sub {{ margin:0 0 8px; font-size:12px; color:var(--ink2); }}
#{cid} .legend {{ display:flex; flex-wrap:wrap; gap:6px 18px; font-size:12px; color:var(--ink2); margin:2px 0 4px; }}
#{cid} .legend span {{ display:inline-flex; align-items:center; gap:6px; }}
#{cid} .key {{ display:inline-block; width:18px; height:0; border-top:2px solid; }}
#{cid} .wash {{ display:inline-block; width:14px; height:10px; background:var(--muted); opacity:.18; }}
#{cid} svg.plot {{ display:block; width:100%; height:auto; overflow:visible; }}
#{cid} .legend svg {{ width:12px; height:10px; display:inline-block; }}
#{cid} text {{ font-family:{FONT}; }}
#{cid} .hz .xh, #{cid} .hz .tip {{ opacity:0; pointer-events:none; }}
#{cid} .hz:hover .xh, #{cid} .hz:hover .tip {{ opacity:1; }}
#{cid} .hit {{ fill:transparent; }}
#{cid} details {{ margin-top:6px; font-size:12px; color:var(--ink2); }}
#{cid} details summary {{ cursor:pointer; }}
#{cid} .tv {{ max-height:260px; overflow:auto; margin-top:6px; border:1px solid var(--border); }}
#{cid} table {{ border-collapse:collapse; width:100%; }}
#{cid} th, #{cid} td {{ padding:2px 10px; text-align:right; white-space:nowrap; }}
#{cid} th:first-child, #{cid} td:first-child {{ text-align:left; }}
#{cid} th {{ position:sticky; top:0; background:var(--surface); border-bottom:1px solid var(--axis); color:var(--ink); }}
#{cid} td {{ font-variant-numeric:tabular-nums; border-bottom:1px solid var(--grid); }}
"""
    out.append(f'<style>{css}</style><div id="{cid}">')
    out.append(f'<h3>{_esc(title)}</h3>')
    if subtitle:
        out.append(f'<p class="sub">{_esc(subtitle)}</p>')
    leg = []
    for s, c in zip(series, colors):
        leg.append(f'<span><i class="key" style="border-color:{c}"></i>{_esc(s["name"])}</span>')
    if bands:
        leg.append(f'<span><i class="wash"></i>{_esc(band_label)}</span>')
    if events:
        leg.append(f'<span><svg width="12" height="10" viewBox="0 0 12 10" style="display:inline"><path d="M6 1 L11 9 H1 Z" fill="var(--ink2)"/></svg>{_esc(event_label)}</span>')
    out.append(f'<div class="legend">{"".join(leg)}</div>')

    s_ = [f'<svg class="plot" viewBox="0 0 {W} {H}" role="img" aria-label="{_esc(title)}">']
    for t in yt:                                                    # hairline solid grid + recessive tick labels
        y = Y(t)
        if mt - 1 <= y <= mt + ph + 1:
            s_.append(f'<line x1="{ml}" x2="{ml + pw}" y1="{y:.1f}" y2="{y:.1f}" stroke="var(--{"axis" if abs(t - lo) < 1e-9 else "grid"})" stroke-width="1"/>')
            s_.append(f'<text x="{ml - 8}" y="{y + 4:.1f}" font-size="11" text-anchor="end" fill="var(--muted)" style="font-variant-numeric:tabular-nums">{_esc(fmt(t) if fmt is not _fmt_default else f"{t:g}")}</text>')
    if y_label:
        s_.append(f'<text x="{ml}" y="{mt - 4}" font-size="11" fill="var(--muted)">{_esc(y_label)}</text>')
    ticks, step = time_ticks(x0, x1)
    for d in ticks:
        x = X(d)
        lab = f'{d:%Y}' if step >= 12 else f'{d:%b %Y}'
        s_.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{mt + ph}" y2="{mt + ph + 4}" stroke="var(--axis)" stroke-width="1"/>')
        s_.append(f'<text x="{x:.1f}" y="{mt + ph + 18}" font-size="11" text-anchor="middle" fill="var(--muted)">{lab}</text>')
    for a, b in (bands or []):                                       # wash bands (e.g. sparse pricing), behind the lines
        xa, xb = X(a), X(b)
        s_.append(f'<rect x="{xa - 1:.1f}" y="{mt}" width="{max(xb - xa, 0) + 2:.1f}" height="{ph}" fill="var(--muted)" opacity="0.16"/>')
    for v, c, s in zip(vals, colors, series):                        # lines, broken at gaps
        d, pen = [], False
        for i in range(n):
            if np.isnan(v[i]):
                pen = False
                continue
            d.append(f'{"L" if pen else "M"}{X(dates.iloc[i]):.1f},{Y(v[i]):.1f}')
            pen = True
        s_.append(f'<path d="{" ".join(d)}" fill="none" stroke="{c}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')
    last_y = []
    for v, c, s in zip(vals, colors, series):                        # end dot (2px surface ring) + selective direct label
        ok = np.where(~np.isnan(v))[0]
        if not len(ok):
            continue
        i = ok[-1]
        x, y = X(dates.iloc[i]), Y(v[i])
        s_.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{c}" stroke="var(--surface)" stroke-width="2"/>')
        last_y.append((y, s, v[i], x))
    for y, s, val, x in last_y:                                      # labels that would collide are dropped (legend + tooltip carry them)
        if all(abs(y - o[0]) >= 15 for o in last_y if o is not (y, s, val, x) and o[1] is not s):
            lab = fmt(val) if len(series) == 1 else f'{s.get("short", s["name"])}  {fmt(val)}'
            s_.append(f'<text x="{x + 10:.1f}" y="{y + 4:.1f}" font-size="12" fill="var(--ink)" font-weight="600">{_esc(lab)}</text>')
    if label_peak and series and (~np.isnan(vals[0])).any():          # the extreme of the first series
        i = int(np.nanargmax(vals[0]))
        x, y = X(dates.iloc[i]), Y(vals[0][i])
        s_.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{colors[0]}" stroke="var(--surface)" stroke-width="2"/>')
        anchor = 'end' if x > ml + pw * 0.8 else 'start'
        s_.append(f'<text x="{x + (-8 if anchor == "end" else 8):.1f}" y="{y - 8:.1f}" font-size="11" text-anchor="{anchor}" fill="var(--ink2)">peak {_esc(fmt(vals[0][i]))} · {dates.iloc[i]:%d %b %Y}</text>')
    for d, txt in (events or []):                                    # event markers on the x axis
        x = X(d)
        s_.append(f'<path d="M{x:.1f},{mt + ph - 1} l-4.5,-8 h9 z" fill="var(--ink2)"><title>{_esc(txt)}</title></path>')
    idx = np.arange(n) if n <= MAX_ZONES else np.unique(np.linspace(0, n - 1, MAX_ZONES).round().astype(int))   # hover zones: crosshair snaps to the nearest date
    xs = np.array([X(dates.iloc[i]) for i in idx])
    edges = np.concatenate([[ml], (xs[:-1] + xs[1:]) / 2, [ml + pw]]) if len(xs) > 1 else np.array([ml, ml + pw])
    tw = 232
    for j, i in enumerate(idx):
        x = xs[j]
        rows = [f'<text x="10" y="17" font-size="11" fill="var(--ink2)">{dates.iloc[i]:%a %d %b %Y}</text>']
        yy = 36
        for v, c, s in zip(vals, colors, series):
            val = fmt(v[i]) if not np.isnan(v[i]) else '-'
            rows.append(f'<line x1="10" x2="26" y1="{yy - 4}" y2="{yy - 4}" stroke="{c}" stroke-width="2" stroke-linecap="round"/>'
                        f'<text x="34" y="{yy}" font-size="12" fill="var(--ink)"><tspan font-weight="600">{_esc(val)}</tspan><tspan fill="var(--ink2)">  {_esc(s["name"])}</tspan></text>')
            yy += 18
        if tip_extra is not None and tip_extra[i]:
            for part in str(tip_extra[i]).split(' · '):          # one short line per fact so nothing runs out of the box
                rows.append(f'<text x="10" y="{yy}" font-size="11" fill="var(--ink2)">{_esc(part)}</text>')
                yy += 15
        th = yy - 8 + 8
        tx = x + 12 if x + 12 + tw < W - 4 else x - 12 - tw
        s_.append(f'<g class="hz"><rect class="hit" x="{edges[j]:.1f}" y="{mt}" width="{max(edges[j + 1] - edges[j], 1):.1f}" height="{ph}"/>'
                  f'<line class="xh" x1="{x:.1f}" x2="{x:.1f}" y1="{mt}" y2="{mt + ph}" stroke="var(--ink2)" stroke-width="1"/>'
                  f'<g class="tip" transform="translate({tx:.1f},{mt + 6})"><rect width="{tw}" height="{th}" rx="6" fill="var(--surface)" stroke="var(--axis)"/>{"".join(rows)}</g></g>')
    s_.append('</svg>')
    out.append(''.join(s_))

    head = ['Date'] + [s['name'] for s in series] + list((table_extra or {}).keys())      # table view: the accessible twin of the chart
    body = []
    for i in range(n):
        cells = [f'{dates.iloc[i]:%Y-%m-%d}'] + [(fmt(v[i]) if not np.isnan(v[i]) else '-') for v in vals] + [str(c[i]) for c in (table_extra or {}).values()]
        body.append('<tr>' + ''.join(f'<td>{_esc(c)}</td>' for c in cells) + '</tr>')
    out.append(f'<details><summary>Table view ({n:,} dates)</summary><div class="tv"><table><thead><tr>{"".join(f"<th>{_esc(h)}</th>" for h in head)}</tr></thead>'
               f'<tbody>{"".join(body)}</tbody></table></div></details></div>')
    return ''.join(out)


def runs(dates, flags):
    """Contiguous runs of True in `flags` as (first_date, last_date) pairs (for shaded bands)."""
    dates = list(pd.to_datetime(pd.Series(list(dates))))
    out, start = [], None
    for i, f in enumerate(list(flags)):
        if f and start is None:
            start = dates[i]
        if (not f or i == len(dates) - 1) and start is not None:
            out.append((start, dates[i] if f else dates[i - 1]))
            start = None
    return out
