// app/charts.js — hand-drawn canvas charts: Monte Carlo band (q05–q95) + median line,
// shared playhead, crosshair tooltip, keyboard access and a table view.
// Colors come only from CSS tokens on :root so both themes work.

const css = () => getComputedStyle(document.documentElement);
export function token(name) { return css().getPropertyValue(name).trim(); }

export function rgba(hex, a) {
  const h = hex.replace('#', '');
  const f = h.length === 3 ? h.split('').map((c) => c + c).join('') : h;
  const n = parseInt(f, 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}

/** Locale number with fixed decimals and thousands separators (−0 shown as 0). */
export function fmt(x, d = 0) {
  if (x === null || x === undefined || !Number.isFinite(x)) return '–';
  const v = Math.abs(x) < 0.5 * 10 ** -d ? 0 : x;
  return v.toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
}
export function fmtSigned(x, d = 0) {
  const s = fmt(x, d);
  return x > 0.5 * 10 ** -d ? `+${s}` : s.replace('-', '−');
}

/** "Nice" tick values covering [lo, hi] (Heckbert). */
export function niceTicks(lo, hi, count = 4) {
  if (!(hi > lo)) { const pad = Math.abs(lo) * 0.01 || 1; lo -= pad; hi += pad; }
  const span = hi - lo;
  const step0 = span / Math.max(1, count);
  const mag = 10 ** Math.floor(Math.log10(step0));
  const err = step0 / mag;
  const step = (err >= 7.5 ? 10 : err >= 3.5 ? 5 : err >= 1.5 ? 2 : 1) * mag;
  const start = Math.floor(lo / step) * step;
  const end = Math.ceil(hi / step) * step;
  const ticks = [];
  for (let v = start; v <= end + step * 1e-9; v += step) ticks.push(+v.toPrecision(12));
  return { ticks, step, lo: start, hi: end };
}
function decimalsForStep(step) { return Math.max(0, -Math.floor(Math.log10(step) + 1e-9)); }

const charts = new Set();
let themeHooked = false;
function hookTheme() {
  if (themeHooked) return; themeHooked = true;
  const redraw = () => charts.forEach((c) => c.draw());
  matchMedia('(prefers-color-scheme: dark)').addEventListener?.('change', redraw);
  new MutationObserver(redraw).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
}

/**
 * Time-series band chart.
 * opts: { unit, decimals, xUnit: 'h'|'d', refLines: [{ value, label }], ariaLabel, onScrub(index) }
 * refLines are classification thresholds: one neutral dashed tone for all of them, never an
 * alarm colour, with the label drawn beside the line (HREQ-S-03, BUG-20261003-101).
 * data: { t: Float64Array (h), series: [{ name, colorVar, q05, q50, q95 }] }
 */
export class BandChart {
  constructor(host, opts = {}) {
    this.host = host; this.opts = opts;
    this.canvas = document.createElement('canvas');
    this.canvas.className = 'chart-canvas';
    this.canvas.tabIndex = 0;
    this.canvas.setAttribute('role', 'img');
    this.canvas.setAttribute('aria-label', opts.ariaLabel || 'chart');
    this.tip = document.createElement('div');
    this.tip.className = 'chart-tip'; this.tip.hidden = true;
    host.append(this.canvas, this.tip);
    this.data = null; this.play = -1; this.hover = -1; this.dim = false;
    this.pad = { l: 44, r: 12, t: 10, b: 24 };
    new ResizeObserver(() => this.draw()).observe(host);
    this.canvas.addEventListener('pointermove', (e) => this.onMove(e));
    this.canvas.addEventListener('pointerleave', () => { this.hover = -1; this.tip.hidden = true; this.draw(); });
    this.canvas.addEventListener('click', () => { if (this.hover >= 0 && opts.onScrub) opts.onScrub(this.hover); });
    this.canvas.addEventListener('keydown', (e) => this.onKey(e));
    this.canvas.addEventListener('blur', () => { this.hover = -1; this.tip.hidden = true; this.draw(); });
    charts.add(this); hookTheme();
  }

  setData(data) { this.data = data; this.hover = -1; this.tip.hidden = true; this.draw(); }
  setPlayhead(i) { if (i !== this.play) { this.play = i; this.draw(); } }
  setDim(d) { this.dim = d; this.host.classList.toggle('is-stale', d); }

  xScale() {
    const t = this.data.t; const w = this.cw - this.pad.l - this.pad.r;
    const t0 = t[0], t1 = t[t.length - 1];
    return (x) => this.pad.l + ((x - t0) / (t1 - t0 || 1)) * w;
  }

  onMove(e) {
    if (!this.data) return;
    const r = this.canvas.getBoundingClientRect();
    const x = e.clientX - r.left;
    const t = this.data.t; const xs = this.xScale();
    // nearest index by binary search on the x position
    let lo = 0, hi = t.length - 1;
    while (hi - lo > 1) { const m = (lo + hi) >> 1; if (xs(t[m]) < x) lo = m; else hi = m; }
    this.hover = Math.abs(xs(t[lo]) - x) < Math.abs(xs(t[hi]) - x) ? lo : hi;
    this.draw(); this.showTip();
  }

  onKey(e) {
    if (!this.data) return;
    const n = this.data.t.length;
    const step = Math.max(1, Math.round(n / 48));
    if (this.hover < 0) this.hover = this.play >= 0 ? this.play : 0;
    if (e.key === 'ArrowRight') this.hover = Math.min(n - 1, this.hover + step);
    else if (e.key === 'ArrowLeft') this.hover = Math.max(0, this.hover - step);
    else if (e.key === 'Enter' && this.opts.onScrub) { this.opts.onScrub(this.hover); return; }
    else return;
    e.preventDefault(); this.draw(); this.showTip();
  }

  timeLabel(h) {
    return this.opts.xUnit === 'd' ? `day ${fmt(h / 24, 1)}` : `${fmt(h, 2)} h`;
  }

  showTip() {
    const i = this.hover; if (i < 0 || !this.data) return;
    const { decimals = 1, unit = '' } = this.opts;
    const tip = this.tip; tip.replaceChildren();
    const head = document.createElement('div'); head.className = 'tip-time';
    head.textContent = this.timeLabel(this.data.t[i]); tip.append(head);
    for (const s of this.data.series) {
      const row = document.createElement('div'); row.className = 'tip-row';
      const key = document.createElement('span'); key.className = 'tip-key';
      key.style.background = token(s.colorVar);
      const val = document.createElement('strong');
      const v = s.q50[i];
      val.textContent = `${this.opts.signed ? fmtSigned(v, decimals) : fmt(v, decimals)} ${unit}`;
      const sub = document.createElement('span'); sub.className = 'tip-sub';
      sub.textContent = `${s.name} · 90% band ${fmt(s.q05[i], decimals)} to ${fmt(s.q95[i], decimals)}`;
      row.append(key, val, sub); tip.append(row);
    }
    const xs = this.xScale(); const x = xs(this.data.t[i]);
    tip.hidden = false;
    const tw = tip.offsetWidth;
    tip.style.left = `${Math.min(Math.max(4, x + 12 > this.cw - tw - 4 ? x - tw - 12 : x + 12), this.cw - tw - 4)}px`;
    tip.style.top = `${this.pad.t + 2}px`;
  }

  draw() {
    const c = this.canvas; const host = this.host;
    const w = host.clientWidth; const h = host.clientHeight || 150;
    if (!w) return;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    if (c.width !== Math.round(w * dpr) || c.height !== Math.round(h * dpr)) {
      c.width = Math.round(w * dpr); c.height = Math.round(h * dpr);
      c.style.width = `${w}px`; c.style.height = `${h}px`;
    }
    this.cw = w; this.ch = h;
    const g = c.getContext('2d');
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);
    const ink2 = token('--ink-2'), muted = token('--muted'), grid = token('--grid'), axis = token('--axis');
    const font = token('--font-body') || 'sans-serif';
    if (!this.data) {
      g.fillStyle = muted; g.font = `12px ${font}`; g.textAlign = 'center';
      g.fillText('Running Monte Carlo…', w / 2, h / 2);
      return;
    }
    const { t, series } = this.data;
    const pad = this.pad; const ph = h - pad.t - pad.b;
    let lo = Infinity, hi = -Infinity;
    for (const s of series) for (let k = 0; k < t.length; k++) { lo = Math.min(lo, s.q05[k]); hi = Math.max(hi, s.q95[k]); }
    for (const r of this.opts.refLines || []) if (r.inRange === false || (r.value >= lo - (hi - lo) * 0.6 && r.value <= hi + (hi - lo) * 0.6)) { lo = Math.min(lo, r.value); hi = Math.max(hi, r.value); }
    if (this.opts.minSpan && hi - lo < this.opts.minSpan) { const m = (hi + lo) / 2; lo = m - this.opts.minSpan / 2; hi = m + this.opts.minSpan / 2; }
    if (this.opts.floor0) lo = Math.min(0, lo);
    const nt = niceTicks(lo, hi, h < 140 ? 3 : 4);
    const y = (v) => pad.t + ph - ((v - nt.lo) / (nt.hi - nt.lo)) * ph;
    const xs = this.xScale();
    const tickDec = decimalsForStep(nt.step);

    // grid + y labels
    g.font = `11px ${font}`; g.textBaseline = 'middle'; g.textAlign = 'right';
    g.lineWidth = 1;
    for (const v of nt.ticks) {
      const yy = Math.round(y(v)) + 0.5;
      g.strokeStyle = grid; g.beginPath(); g.moveTo(pad.l, yy); g.lineTo(w - pad.r, yy); g.stroke();
      g.fillStyle = muted; g.fillText(fmt(v, tickDec), pad.l - 6, yy);
    }
    // x ticks
    const xUnitDays = this.opts.xUnit === 'd';
    const tx = niceTicks(t[0] / (xUnitDays ? 24 : 1), t[t.length - 1] / (xUnitDays ? 24 : 1), Math.max(3, Math.floor(w / 80)));
    g.textAlign = 'center'; g.textBaseline = 'top';
    for (const v of tx.ticks) {
      const hh = xUnitDays ? v * 24 : v;
      if (hh < t[0] - 1e-9 || hh > t[t.length - 1] + 1e-9) continue;
      const xx = Math.round(xs(hh)) + 0.5;
      g.strokeStyle = axis; g.beginPath(); g.moveTo(xx, pad.t + ph); g.lineTo(xx, pad.t + ph + 4); g.stroke();
      const suffix = { d: ' d', g: ' g', h: ' h' }[this.opts.xUnit || 'h'];
      const first = v === tx.ticks.find((q) => (xUnitDays ? q * 24 : q) >= t[0] - 1e-9);
      g.fillStyle = muted; g.fillText(`${fmt(v, decimalsForStep(tx.step))}${first ? suffix : ''}`, xx, pad.t + ph + 6);
    }
    // baseline axis
    g.strokeStyle = axis; g.beginPath(); g.moveTo(pad.l, Math.round(pad.t + ph) + 0.5); g.lineTo(w - pad.r, Math.round(pad.t + ph) + 0.5); g.stroke();

    // reference lines (their labels are drawn over the series, below)
    const refs = (this.opts.refLines || []).filter((r) => r.value >= nt.lo && r.value <= nt.hi);
    for (const r of refs) {
      const yy = Math.round(y(r.value)) + 0.5;
      g.strokeStyle = muted;
      g.setLineDash([4, 3]); g.lineWidth = 1;
      g.beginPath(); g.moveTo(pad.l, yy); g.lineTo(w - pad.r, yy); g.stroke();
      g.setLineDash([]);
    }

    // bands then medians
    const step = Math.max(1, Math.floor(t.length / (w * 1.5)));
    for (const s of series) {
      const col = token(s.colorVar);
      g.fillStyle = rgba(col, this.dim ? 0.08 : 0.16);
      g.beginPath();
      for (let k = 0; k < t.length; k += step) { const X = xs(t[k]); k === 0 ? g.moveTo(X, y(s.q95[k])) : g.lineTo(X, y(s.q95[k])); }
      g.lineTo(xs(t[t.length - 1]), y(s.q95[t.length - 1]));
      for (let k = t.length - 1; k >= 0; k -= step) g.lineTo(xs(t[k]), y(s.q05[k]));
      g.lineTo(xs(t[0]), y(s.q05[0]));
      g.closePath(); g.fill();
    }
    for (const s of series) {
      const col = token(s.colorVar);
      g.strokeStyle = this.dim ? rgba(col, 0.45) : col; g.lineWidth = 2; g.lineJoin = 'round'; g.lineCap = 'round';
      g.beginPath();
      for (let k = 0; k < t.length; k += step) { const X = xs(t[k]); k === 0 ? g.moveTo(X, y(s.q50[k])) : g.lineTo(X, y(s.q50[k])); }
      g.lineTo(xs(t[t.length - 1]), y(s.q50[t.length - 1]));
      g.stroke();
    }

    // playhead
    const surf = token('--surface');
    const mark = (i, strong) => {
      const X = Math.round(xs(t[i])) + 0.5;
      g.strokeStyle = strong ? token('--ink') : axis; g.lineWidth = 1;
      g.beginPath(); g.moveTo(X, pad.t); g.lineTo(X, pad.t + ph); g.stroke();
      for (const s of series) {
        g.beginPath(); g.arc(X, y(s.q50[i]), 4, 0, Math.PI * 2);
        g.fillStyle = token(s.colorVar); g.fill();
        g.lineWidth = 2; g.strokeStyle = surf; g.stroke();
      }
    };
    if (this.play >= 0 && this.play < t.length) mark(this.play, true);
    if (this.hover >= 0 && this.hover !== this.play) mark(this.hover, false);

    // reference-line labels last, on a surface halo, so no band, median or playhead hides them
    g.textAlign = 'right'; g.textBaseline = 'bottom'; g.lineJoin = 'round'; g.lineWidth = 3; g.strokeStyle = surf; g.fillStyle = ink2;
    for (const r of refs) {
      const yy = Math.round(y(r.value)) + 0.5; const maxW = w - pad.l - pad.r - 4;   // condensed, never clipped
      g.strokeText(r.label, w - pad.r - 2, yy - 2, maxW); g.fillText(r.label, w - pad.r - 2, yy - 2, maxW);
    }
  }

  /** Rows for the table view: one per whole hour (or day for chronic). */
  tableRows() {
    if (!this.data) return [];
    const { t } = this.data; const every = this.opts.xUnit === 'd' ? 24 : (t[t.length - 1] > 30 ? 4 : 1);
    const rows = []; let next = t[0];
    for (let k = 0; k < t.length; k++) {
      if (t[k] + 1e-9 >= next) { rows.push(k); next = Math.round(t[k] / every) * every + every; }
    }
    return rows;
  }
}

/**
 * Dose–response chart: x = dose (g NaCl), band + median + 8px markers at each dose.
 * data: { values: number[], q05, q50, q95, colorVar }
 */
export class DoseChart extends BandChart {
  setData(d) {
    this.data = d && { t: Float64Array.from(d.values), series: [{ name: d.name, colorVar: d.colorVar,
      q05: d.q05, q50: d.q50, q95: d.q95 }] };
    this.hover = -1; this.tip.hidden = true; this.draw();
  }
  timeLabel(g) { return `${fmt(g, 1)} g salt`; }
  draw() {
    super.draw();
    if (!this.data) return;
    // markers at every dose, with a surface ring
    const g = this.canvas.getContext('2d');
    const { t, series } = this.data; const s = series[0];
    const xs = this.xScale();
    const pad = this.pad; const ph = this.ch - pad.t - pad.b;
    let lo = Infinity, hi = -Infinity;
    for (let k = 0; k < t.length; k++) { lo = Math.min(lo, s.q05[k]); hi = Math.max(hi, s.q95[k]); }
    if (this.opts.floor0) lo = Math.min(0, lo);
    const nt = niceTicks(lo, hi, this.ch < 140 ? 3 : 4);
    const y = (v) => pad.t + ph - ((v - nt.lo) / (nt.hi - nt.lo)) * ph;
    for (let k = 0; k < t.length; k++) {
      g.beginPath(); g.arc(xs(t[k]), y(s.q50[k]), k === this.hover ? 5 : 4, 0, Math.PI * 2);
      g.fillStyle = token(s.colorVar); g.fill(); g.lineWidth = 2; g.strokeStyle = token('--surface'); g.stroke();
    }
    // direct label at the last point (single series: no legend)
    const last = t.length - 1;
    g.fillStyle = token('--ink'); g.font = `600 11px ${token('--font-body')}`; g.textAlign = 'right'; g.textBaseline = 'bottom';
    g.fillText(fmt(s.q50[last], this.opts.decimals ?? 2), xs(t[last]) - 6, y(s.q50[last]) - 6);
  }
}
