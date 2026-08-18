/* mec-cast telemetry dashboard.
 *
 * Reads three endpoints and draws the session:
 *   GET /api/v1/sessions?since&until      the picker
 *   GET /api/v1/sessions/{id}?slo_ms      aggregate statistics
 *   GET /api/v1/sessions/{id}/timeseries  per-window columns
 *
 * Everything the recorder stores is a per-window aggregate, so the wording
 * here is deliberate: "typical" is the median across windows, never a
 * session-wide percentile, and the histogram counts windows, not frames.
 */
'use strict';

const API = '/api/v1';
const $ = (id) => document.getElementById(id);

const state = { sessions: [], detail: null, series: null, charts: [] };

/* ── formatting ─────────────────────────────────────────────────────── */

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

/** Nanoseconds to a human unit, keeping three significant figures. */
function ns(value, digits = 1) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  const abs = Math.abs(value);
  if (abs < 1e3) return `${value.toFixed(0)} ns`;
  if (abs < 1e6) return `${(value / 1e3).toFixed(digits)} µs`;
  if (abs < 1e9) return `${(value / 1e6).toFixed(digits)} ms`;
  return `${(value / 1e9).toFixed(2)} s`;
}

const ms = (value) => (value === null || value === undefined ? null : value / 1e6);

function clock(seconds) {
  if (seconds === null || seconds === undefined) return '—';
  const total = Math.round(seconds);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}

function duration(seconds) {
  if (!seconds || seconds < 1) return '<1 s';
  if (seconds < 90) return `${seconds.toFixed(0)} s`;
  if (seconds < 5400) return `${(seconds / 60).toFixed(1)} min`;
  return `${(seconds / 3600).toFixed(1)} h`;
}

const when = (iso) =>
  new Date(iso).toLocaleString(undefined, {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit',
  });

const pct = (value, digits = 0) =>
  value === null || value === undefined ? '—' : `${value.toFixed(digits)}%`;

const escapeHtml = (text) =>
  String(text).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/* ── data loading ───────────────────────────────────────────────────── */

async function getJSON(url) {
  const response = await fetch(url);
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body.detail) detail = body.detail;
    } catch { /* keep the status line */ }
    throw new Error(detail);
  }
  return response.json();
}

function windowBounds() {
  const choice = $('period').value;
  if (choice === 'custom') {
    const from = $('fromAt').value;
    const to = $('toAt').value;
    return {
      since: from ? new Date(from).toISOString() : undefined,
      until: to ? new Date(to).toISOString() : undefined,
    };
  }
  const until = new Date();
  const since = new Date(until.getTime() - Number(choice) * 86400_000);
  return { since: since.toISOString(), until: until.toISOString() };
}

async function loadSessions() {
  const { since, until } = windowBounds();
  const query = new URLSearchParams();
  if (since) query.set('since', since);
  if (until) query.set('until', until);
  query.set('limit', '200');

  const data = await getJSON(`${API}/sessions?${query}`);
  state.sessions = data.items;

  const picker = $('session');
  picker.innerHTML = '';
  if (!data.items.length) {
    $('empty').classList.remove('hidden');
    $('content').classList.add('hidden');
    picker.innerHTML = '<option>No sessions</option>';
    $('sessionCaption').textContent = 'No sessions in this period.';
    return null;
  }
  $('empty').classList.add('hidden');

  for (const session of data.items) {
    const option = document.createElement('option');
    option.value = session.trace_id;
    const g2g = session.e2e_p50_typical_ns ? ns(session.e2e_p50_typical_ns, 0) : 'no g2g';
    option.textContent =
      `${when(session.started_at)} · ${duration(session.duration_s)} · ${g2g} · ` +
      `${session.trace_id.slice(0, 8)}`;
    picker.appendChild(option);
  }
  return data.items[0].trace_id;
}

async function loadSession(traceId) {
  const slo = $('slo').value;
  const [detail, series] = await Promise.all([
    getJSON(`${API}/sessions/${encodeURIComponent(traceId)}?slo_ms=${slo}`),
    getJSON(`${API}/sessions/${encodeURIComponent(traceId)}/timeseries`),
  ]);
  state.detail = detail;
  state.series = series;
  render();
}

/* ── verdict ────────────────────────────────────────────────────────── */

/** Weather from p99 against the chosen budget: how it *feels* to operate. */
function weather(p99Ns, budgetNs) {
  if (p99Ns === null || p99Ns === undefined) {
    return { glyph: '🌫️', word: 'No reading', sub: 'This session reported no glass-to-glass metric.' };
  }
  const ratio = p99Ns / budgetNs;
  if (ratio <= 0.5) return { glyph: '☀️', word: 'Buttery smooth', sub: 'p99 sits at half the budget or better.' };
  if (ratio <= 0.85) return { glyph: '🌤️', word: 'Comfortable', sub: 'Inside budget with room to spare.' };
  if (ratio <= 1.0) return { glyph: '⛅', word: 'Just inside', sub: 'Meeting budget, but with little headroom.' };
  if (ratio <= 1.5) return { glyph: '🌧️', word: 'Choppy', sub: 'p99 is over budget — operators will feel this.' };
  if (ratio <= 3) return { glyph: '⛈️', word: 'Rough', sub: 'Well over budget. Teleoperation would struggle.' };
  return { glyph: '🌋', word: 'Unusable', sub: 'Multiples over budget.' };
}

function trophies(detail) {
  const out = [];
  const e2e = detail.metrics.e2e;
  const dropped = detail.by_service.reduce((sum, s) => sum + s.samples_dropped, 0);
  const missing = detail.by_service.reduce((sum, s) => sum + (s.frames_missing || 0), 0);

  if (e2e && e2e.p99_worst_ns < 50e6) out.push(['ok', '🏅 sub-50 ms club']);
  if (e2e && e2e.p99_worst_ns < 20e6) out.push(['ok', '🚀 sub-20 ms club']);
  if (dropped === 0 && missing === 0) out.push(['ok', '💎 flawless — zero frames lost']);
  if (detail.ptp.trustworthy) out.push(['ok', '🔒 PTP-clean run']);
  if (detail.slo_compliance_pct === 100) out.push(['ok', '🎯 100% in budget']);
  if (detail.duration_s > 3600) out.push(['info', '🕐 marathon — over an hour']);
  if (e2e && e2e.samples > 100_000) out.push(['info', `📊 ${(e2e.samples / 1000).toFixed(0)}k frames`]);
  if (detail.p99_drift_ns_per_min !== null && Math.abs(detail.p99_drift_ns_per_min) > 1e6) {
    const rising = detail.p99_drift_ns_per_min > 0;
    out.push([rising ? 'warn' : 'ok',
      `${rising ? '📈' : '📉'} p99 ${rising ? 'drifting up' : 'improving'} ` +
      `${ns(Math.abs(detail.p99_drift_ns_per_min), 1)}/min`]);
  }
  if (!out.length) out.push(['muted', 'no badges this run']);
  return out;
}

/* ── hand-drawn SVG figures ─────────────────────────────────────────── */

function drawGauge(p99Ns, budgetNs) {
  const svg = $('gauge');
  const ratio = p99Ns === null || p99Ns === undefined ? null : p99Ns / budgetNs;
  const cx = 100, cy = 96, r = 74;
  // Half dial spanning 180°, clamped at 1.6x budget so overshoot stays visible.
  const clamped = ratio === null ? 0 : Math.min(ratio, 1.6) / 1.6;
  const angle = Math.PI * (1 - clamped);
  const arc = (from, to, colour, width) => {
    const a0 = Math.PI * (1 - from), a1 = Math.PI * (1 - to);
    // The dial spans 180°, so no segment of it can ever be a large arc.
    return `<path d="M ${cx + r * Math.cos(a0)} ${cy - r * Math.sin(a0)}
             A ${r} ${r} 0 0 1 ${cx + r * Math.cos(a1)} ${cy - r * Math.sin(a1)}"
             fill="none" stroke="${colour}" stroke-width="${width}" stroke-linecap="round"/>`;
  };

  const budgetMark = 1 / 1.6;
  svg.innerHTML = `
    ${arc(0, budgetMark, css('--green'), 11)}
    ${arc(budgetMark, 1, css('--rose'), 11)}
    <line x1="${cx + (r - 16) * Math.cos(Math.PI * (1 - budgetMark))}"
          y1="${cy - (r - 16) * Math.sin(Math.PI * (1 - budgetMark))}"
          x2="${cx + (r + 8) * Math.cos(Math.PI * (1 - budgetMark))}"
          y2="${cy - (r + 8) * Math.sin(Math.PI * (1 - budgetMark))}"
          stroke="${css('--text-faint')}" stroke-width="2"/>
    ${ratio === null ? '' : `
      <line x1="${cx}" y1="${cy}" x2="${cx + (r - 20) * Math.cos(angle)}"
            y2="${cy - (r - 20) * Math.sin(angle)}"
            stroke="${css('--text')}" stroke-width="3.5" stroke-linecap="round"/>
      <circle cx="${cx}" cy="${cy}" r="6" fill="${css('--text')}"/>`}
    <text x="${cx}" y="${cy - 26}" text-anchor="middle" font-size="21"
          font-family="${css('--mono')}" font-weight="600" fill="${css('--text')}">
      ${ratio === null ? '—' : ns(p99Ns, 0)}
    </text>
    <text x="${cx}" y="${cy + 18}" text-anchor="middle" font-size="11"
          fill="${css('--text-faint')}">budget ${budgetNs / 1e6} ms</text>`;
}

function drawDonut(budget) {
  const svg = $('donut');
  if (!budget) {
    svg.innerHTML = `<text x="160" y="100" text-anchor="middle" fill="${css('--text-faint')}"
      font-size="13">No glass-to-glass metric in this session.</text>`;
    $('donutHint').textContent = '';
    return;
  }
  const parts = [
    ['Sender', Math.max(budget.sender_ns, 0), css('--violet')],
    ['Network', Math.max(budget.network_ns, 0), css('--blue')],
    ['Processing', Math.max(budget.processing_ns, 0), css('--accent')],
    ['Unaccounted', Math.max(budget.unaccounted_ns, 0), css('--text-faint')],
  ].filter(([, value]) => value > 0);

  const total = parts.reduce((sum, [, value]) => sum + value, 0);
  const cx = 92, cy = 100, outer = 68, inner = 42;
  let angle = -Math.PI / 2;
  let paths = '';

  for (const [, value, colour] of parts) {
    const sweep = (value / total) * Math.PI * 2;
    const end = angle + sweep;
    const large = sweep > Math.PI ? 1 : 0;
    // A full circle cannot be drawn as one arc; nudge it closed instead.
    const stop = sweep >= Math.PI * 2 - 1e-6 ? end - 1e-4 : end;
    paths += `<path d="
      M ${cx + outer * Math.cos(angle)} ${cy + outer * Math.sin(angle)}
      A ${outer} ${outer} 0 ${large} 1 ${cx + outer * Math.cos(stop)} ${cy + outer * Math.sin(stop)}
      L ${cx + inner * Math.cos(stop)} ${cy + inner * Math.sin(stop)}
      A ${inner} ${inner} 0 ${large} 0 ${cx + inner * Math.cos(angle)} ${cy + inner * Math.sin(angle)}
      Z" fill="${colour}" stroke="${css('--bg-raised')}" stroke-width="1.5"/>`;
    angle = end;
  }

  const rows = parts.map(([label, value, colour], index) => `
    <rect x="196" y="${44 + index * 26}" width="10" height="10" rx="2.5" fill="${colour}"/>
    <text x="213" y="${53 + index * 26}" font-size="12" fill="${css('--text-muted')}">${label}</text>
    <text x="316" y="${53 + index * 26}" font-size="12" text-anchor="end"
          font-family="${css('--mono')}" fill="${css('--text')}">${((value / total) * 100).toFixed(0)}%</text>
  `).join('');

  svg.innerHTML = `${paths}
    <text x="${cx}" y="${cy - 4}" text-anchor="middle" font-size="17" font-weight="600"
          font-family="${css('--mono')}" fill="${css('--text')}">${ns(budget.total_ns, 0)}</text>
    <text x="${cx}" y="${cy + 14}" text-anchor="middle" font-size="10"
          fill="${css('--text-faint')}">mean g2g</text>
    ${rows}`;

  const share = (budget.unaccounted_ns / budget.total_ns) * 100;
  $('donutHint').textContent = Math.abs(share) > 15
    ? `Unaccounted is ${share.toFixed(0)}% of the mean — the three stages come from ` +
      'independent windows and PTP offset lands here too, so a share this large usually means ' +
      'the clocks disagree rather than that time went missing.'
    : 'Stages are measured in independent windows, so a small residual is expected.';
}

function drawHistogram(values) {
  const svg = $('histogram');
  const usable = values.filter((v) => v !== null && v !== undefined);
  if (usable.length < 2) {
    svg.innerHTML = `<text x="320" y="120" text-anchor="middle" fill="${css('--text-faint')}"
      font-size="13">Not enough windows to bin.</text>`;
    return;
  }

  const min = Math.min(...usable), max = Math.max(...usable);
  const bins = Math.min(24, Math.max(6, Math.ceil(Math.sqrt(usable.length))));
  const width = (max - min) || 1;
  const counts = new Array(bins).fill(0);
  for (const value of usable) {
    counts[Math.min(bins - 1, Math.floor(((value - min) / width) * bins))] += 1;
  }

  const peak = Math.max(...counts);
  const left = 46, right = 622, top = 16, bottom = 200;
  const spanX = right - left, spanY = bottom - top;
  const barWidth = spanX / bins;

  const bars = counts.map((count, index) => {
    const height = peak ? (count / peak) * spanY : 0;
    return `<rect x="${left + index * barWidth + 1.5}" y="${bottom - height}"
             width="${barWidth - 3}" height="${height}" rx="2.5" fill="${css('--accent')}"
             opacity="${0.55 + 0.45 * (count / peak)}"><title>${count} window${
               count === 1 ? '' : 's'}</title></rect>`;
  }).join('');

  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => {
    const x = left + f * spanX;
    return `<line x1="${x}" y1="${bottom}" x2="${x}" y2="${bottom + 5}" stroke="${css('--border-strong')}"/>
            <text x="${x}" y="${bottom + 20}" text-anchor="middle" font-size="11"
                  font-family="${css('--mono')}" fill="${css('--text-faint')}">${ns(min + f * width, 0)}</text>`;
  }).join('');

  svg.innerHTML = `
    <line x1="${left}" y1="${bottom}" x2="${right}" y2="${bottom}" stroke="${css('--border-strong')}"/>
    ${bars}${ticks}
    <text x="${left}" y="${top + 4}" font-size="11" fill="${css('--text-faint')}">${peak} windows</text>
    <text x="334" y="234" text-anchor="middle" font-size="11" fill="${css('--text-faint')}">
      window median glass-to-glass</text>`;
}

function drawBarcode(series) {
  const svg = $('barcode');
  const drops = series.samples_delta;
  const worst = Math.max(1, ...drops);
  const width = 1000 / Math.max(drops.length, 1);

  svg.innerHTML = drops.map((count, index) => {
    const clean = count === 0;
    const intensity = clean ? 0 : 0.35 + 0.65 * (count / worst);
    return `<rect x="${index * width}" y="0" width="${Math.max(width - 0.5, 0.5)}" height="64"
             fill="${clean ? css('--green') : css('--rose')}"
             opacity="${clean ? 0.22 : intensity}"><title>${
               clock(series.elapsed_s[index])} — ${count} dropped</title></rect>`;
  }).join('');

  // The stripes only show ring drops. Frames that never reached the recorder
  // are invisible here, so the badge must not claim the run was clean.
  const total = drops.reduce((sum, value) => sum + value, 0);
  const badWindows = drops.filter((value) => value > 0).length;
  const missing = state.detail.by_service.reduce((sum, s) => sum + (s.frames_missing || 0), 0);

  const parts = [];
  parts.push(total === 0
    ? 'no ring drops'
    : `${total.toLocaleString()} dropped in ${badWindows} window${badWindows === 1 ? '' : 's'}`);
  if (missing > 0) parts.push(`${missing.toLocaleString()} never arrived`);

  const badge = $('dropSummary');
  badge.textContent = parts.join(' · ');
  badge.className = `badge ${missing > 0 ? 'bad' : total > 0 ? 'warn' : 'muted'}`;
}

/* ── uPlot charts ───────────────────────────────────────────────────── */

function chartSize(element, height) {
  return { width: Math.max(element.clientWidth || 600, 260), height };
}

const elapsedAxis = () => ({
  stroke: css('--text-faint'),
  grid: { stroke: css('--grid'), width: 1 },
  ticks: { stroke: css('--border-strong'), size: 4 },
  values: (_, splits) => splits.map(clock),
  font: `11px ${css('--mono')}`,
});

const msAxis = (label) => ({
  scale: 'ms',
  stroke: css('--text-faint'),
  grid: { stroke: css('--grid'), width: 1 },
  ticks: { stroke: css('--border-strong'), size: 4 },
  font: `11px ${css('--mono')}`,
  label,
  labelSize: 26,
  labelFont: `11px ${css('--sans')}`,
});

function drawG2G(series, budgetMs) {
  const target = $('g2gChart');
  target.innerHTML = '';
  const x = series.elapsed_s;

  // A lossy run reports e2e in scattered windows. A reading with a gap on both
  // sides is a zero-length line segment, which draws as nothing — the panel
  // then looks broken rather than sparse. Whenever any reading is isolated
  // like that, turn on point markers so every window that reported is visible.
  const values = series.e2e_p50_ns;
  const filled = values.filter((v) => v !== null).length;
  const isolated = values.some((v, i) =>
    v !== null && (i === 0 || values[i - 1] === null) && (i === values.length - 1 || values[i + 1] === null));
  const points = isolated ? { show: true, size: 5 } : {};
  const data = [
    x,
    series.e2e_max_ns.map(ms),
    series.e2e_min_ns.map(ms),
    series.e2e_p99_ns.map(ms),
    series.e2e_p90_ns.map(ms),
    series.e2e_p50_ns.map(ms),
  ];

  const options = {
    ...chartSize(target, 300),
    scales: { x: { time: false }, ms: {} },
    legend: { live: true },
    cursor: { drag: { x: true, y: false } },
    axes: [elapsedAxis(), msAxis('milliseconds')],
    bands: [{ series: [1, 2], fill: `color-mix(in srgb, ${css('--accent')} 12%, transparent)` }],
    series: [
      { label: 'elapsed', value: (_, v) => clock(v) },
      { label: 'max', scale: 'ms', stroke: `color-mix(in srgb, ${css('--accent')} 45%, transparent)`,
        width: 1, value: (_, v) => (v === null ? '—' : `${v.toFixed(2)} ms`) },
      { label: 'min', scale: 'ms', stroke: `color-mix(in srgb, ${css('--accent')} 45%, transparent)`,
        width: 1, value: (_, v) => (v === null ? '—' : `${v.toFixed(2)} ms`) },
      { label: 'p99', scale: 'ms', stroke: css('--rose'), width: 1.6, points,
        value: (_, v) => (v === null ? '—' : `${v.toFixed(2)} ms`) },
      { label: 'p90', scale: 'ms', stroke: css('--amber'), width: 1.6, points,
        value: (_, v) => (v === null ? '—' : `${v.toFixed(2)} ms`) },
      { label: 'p50', scale: 'ms', stroke: css('--accent'), width: 2.2, points,
        value: (_, v) => (v === null ? '—' : `${v.toFixed(2)} ms`) },
    ],
    hooks: {
      draw: [(chart) => {
        // Budget line, drawn over the plot so it reads as a threshold.
        const y = chart.valToPos(budgetMs, 'ms', true);
        if (!Number.isFinite(y)) return;
        const ctx = chart.ctx;
        ctx.save();
        ctx.strokeStyle = css('--rose');
        ctx.setLineDash([5, 4]);
        ctx.lineWidth = 1.2;
        ctx.beginPath();
        ctx.moveTo(chart.bbox.left, y);
        ctx.lineTo(chart.bbox.left + chart.bbox.width, y);
        ctx.stroke();
        ctx.restore();
      }],
    },
  };
  state.charts.push(new uPlot(options, data, target));

  const badge = $('g2gWindows');
  const total = series.elapsed_s.length;
  badge.textContent = filled === total
    ? `${total} windows`
    : `${filled} of ${total} windows reported`;
  badge.className = `badge ${filled === total ? 'muted' : 'warn'}`;
}

function drawBudgetSeries(series) {
  const target = $('budgetChart');
  target.innerHTML = '';
  // Cumulative so the areas stack: sender, then +network, then +processing.
  const sender = series.sender_mean_ns.map(ms);
  const network = series.network_mean_ns.map(ms);
  const processing = series.processing_mean_ns.map(ms);
  const add = (a, b) => a.map((value, i) => (value === null && b[i] === null
    ? null : (value || 0) + (b[i] || 0)));

  const level1 = sender;
  const level2 = add(level1, network);
  const level3 = add(level2, processing);

  const options = {
    ...chartSize(target, 240),
    scales: { x: { time: false }, ms: {} },
    legend: { live: true },
    axes: [elapsedAxis(), msAxis('milliseconds')],
    bands: [
      { series: [3, 2], fill: `color-mix(in srgb, ${css('--accent')} 30%, transparent)` },
      { series: [2, 1], fill: `color-mix(in srgb, ${css('--blue')} 30%, transparent)` },
    ],
    series: [
      { label: 'elapsed', value: (_, v) => clock(v) },
      { label: 'sender', scale: 'ms', stroke: css('--violet'), width: 1.4, fill:
        `color-mix(in srgb, ${css('--violet')} 30%, transparent)`,
        value: (_, v) => (v === null ? '—' : `${v.toFixed(2)} ms`) },
      { label: '+network', scale: 'ms', stroke: css('--blue'), width: 1.4,
        value: (_, v) => (v === null ? '—' : `${v.toFixed(2)} ms`) },
      { label: '+processing', scale: 'ms', stroke: css('--accent'), width: 1.4,
        value: (_, v) => (v === null ? '—' : `${v.toFixed(2)} ms`) },
    ],
  };
  state.charts.push(new uPlot(options, [series.elapsed_s, level1, level2, level3], target));

  $('budgetLegend').innerHTML = [
    ['Sender', '--violet'], ['Network', '--blue'], ['Processing', '--accent'],
  ].map(([label, token]) =>
    `<span><i class="swatch" style="background:${css(token)}"></i>${label}</span>`).join('');
}

/** A disabled PtpMonitor reports offset 0 / unreliable every window, which is
 *  "this host has no PTP" rather than "PTP is drifting". They read the same in
 *  the data and must not read the same on the page. */
function ptpMonitorDisabled(series) {
  return series.ptp_reliable.every((ok) => !ok)
    && series.ptp_offset_ns.every((v) => v === 0 || v === null);
}

function drawPtp(series) {
  const target = $('ptpChart');
  target.innerHTML = '';
  const offsets = series.ptp_offset_ns.map((v) => (v === null ? null : v / 1e3));
  const hasReading = offsets.some((v) => v !== null);

  if (ptpMonitorDisabled(series)) {
    target.innerHTML = `<p class="empty" style="padding:28px">
      No PTP monitor on this run — the recorder reported a disabled clock source.</p>`;
    $('ptpHint').innerHTML =
      'Every window reported offset 0 with no lock, which is what a <em>disabled</em> monitor ' +
      'emits: this host has no PTP hardware, or the run was same-host. Glass-to-glass and ' +
      'network subtract stamps taken on two machines, so treat them as indicative only. ' +
      'Sender and processing stay within one host and remain valid.';
    return;
  }

  if (!hasReading) {
    target.innerHTML = `<p class="empty" style="padding:28px">No PTP readings on this run.</p>`;
  } else {
    const options = {
      ...chartSize(target, 200),
      scales: { x: { time: false }, us: {} },
      legend: { live: true },
      axes: [elapsedAxis(), { ...msAxis('µs offset'), scale: 'us' }],
      series: [
        { label: 'elapsed', value: (_, v) => clock(v) },
        { label: 'offset', scale: 'us', stroke: css('--amber'), width: 1.6,
          value: (_, v) => (v === null ? '—' : `${v.toFixed(1)} µs`) },
      ],
      hooks: {
        draw: [(chart) => {
          // Shade windows the monitor called unreliable.
          const ctx = chart.ctx;
          ctx.save();
          ctx.fillStyle = `color-mix(in srgb, ${css('--rose')} 16%, transparent)`;
          series.ptp_reliable.forEach((ok, index) => {
            if (ok) return;
            const x0 = chart.valToPos(series.elapsed_s[index], 'x', true);
            const next = series.elapsed_s[index + 1] ?? series.elapsed_s[index];
            const x1 = chart.valToPos(next, 'x', true);
            ctx.fillRect(x0, chart.bbox.top, Math.max(x1 - x0, 1.5), chart.bbox.height);
          });
          ctx.restore();
        }],
      },
    };
    state.charts.push(new uPlot(options, [series.elapsed_s, offsets], target));
  }

  const ptp = state.detail.ptp;
  $('ptpHint').innerHTML = ptp.trustworthy
    ? `Locked for all ${ptp.windows} windows — glass-to-glass and network are trustworthy.`
    : `<strong>Only ${pct(ptp.reliable_pct)} of windows had a reliable lock.</strong> ` +
      'Glass-to-glass and network are differences between two hosts\' clocks, so unshaded ' +
      'regions measure clock offset rather than latency. Red bands mark unreliable windows.';
}

/* ── tables ─────────────────────────────────────────────────────────── */

const METRIC_LABELS = {
  e2e: 'Glass-to-glass',
  network: 'Network',
  processing: 'Processing',
  sender: 'Sender',
};

function fillMetrics(detail) {
  const body = $('metricsTable').querySelector('tbody');
  const order = ['e2e', 'sender', 'network', 'processing'];
  body.innerHTML = order.filter((key) => detail.metrics[key]).map((key) => {
    const m = detail.metrics[key];
    return `<tr>
      <td class="name">${METRIC_LABELS[key]}</td>
      <td>${m.samples.toLocaleString()}</td>
      <td>${ns(m.mean_ns)}</td>
      <td>${ns(m.stddev_ns)}</td>
      <td>${ns(m.p50_typical_ns)}</td>
      <td>${ns(m.p90_typical_ns)}</td>
      <td>${ns(m.p99_typical_ns)}</td>
      <td>${ns(m.p99_worst_ns)}</td>
      <td>${ns(m.max_ns)}</td>
    </tr>`;
  }).join('') || `<tr><td colspan="9" class="dim">No metrics reported.</td></tr>`;
}

function fillServices(detail) {
  const body = $('serviceTable').querySelector('tbody');
  body.innerHTML = detail.by_service.map((s) => {
    const lost = (s.samples_dropped || 0) + (s.frames_missing || 0);
    const rate = s.frames_expected ? (lost / s.frames_expected) * 100 : null;
    const cls = rate === null ? '' : rate === 0 ? 'ok' : rate < 1 ? 'warn' : 'bad';
    return `<tr>
      <td class="name">${escapeHtml(s.service)}${
        s.host ? `<span class="dim"> · ${escapeHtml(s.host)}</span>` : ''}</td>
      <td>${s.windows}</td>
      <td>${s.rows_written.toLocaleString()}</td>
      <td>${s.samples_dropped.toLocaleString()}</td>
      <td>${s.frames_missing === null ? '—' : s.frames_missing.toLocaleString()}</td>
      <td>${rate === null ? '—' : `<span class="badge ${cls}">${rate.toFixed(2)}%</span>`}</td>
    </tr>`;
  }).join('');
}

function fillShame(series) {
  const body = $('shameTable').querySelector('tbody');
  const rows = series.elapsed_s
    .map((elapsed, index) => ({
      index,
      elapsed,
      at: series.t[index],
      p99: series.e2e_p99_ns[index],
      max: series.e2e_max_ns[index],
      dropped: series.samples_delta[index],
      reliable: series.ptp_reliable[index],
    }))
    .filter((row) => row.p99 !== null)
    .sort((a, b) => b.p99 - a.p99)
    .slice(0, 5);

  body.innerHTML = rows.length
    ? rows.map((row, rank) => `<tr>
        <td class="name">${rank + 1}</td>
        <td>${when(new Date(row.at * 1000).toISOString())}</td>
        <td>${clock(row.elapsed)}</td>
        <td>${ns(row.p99)}</td>
        <td>${ns(row.max)}</td>
        <td>${row.dropped.toLocaleString()}</td>
        <td>${row.reliable
          ? '<span class="badge ok">locked</span>'
          : '<span class="badge bad">unlocked</span>'}</td>
      </tr>`).join('')
    : `<tr><td colspan="7" class="dim">No glass-to-glass readings in this session.</td></tr>`;
}

/* ── render ─────────────────────────────────────────────────────────── */

function render() {
  const detail = state.detail;
  const series = state.series;
  const budgetNs = detail.slo_threshold_ns;

  state.charts.forEach((chart) => chart.destroy());
  state.charts = [];

  $('content').classList.remove('hidden');
  $('sessionCaption').innerHTML =
    `<code>${escapeHtml(detail.trace_id)}</code> · ${when(detail.started_at)} · ` +
    `${duration(detail.duration_s)} · ${detail.windows} windows · ` +
    `${detail.services.map(escapeHtml).join(', ')}`;

  const e2e = detail.metrics.e2e || null;
  const worstP99 = e2e ? e2e.p99_worst_ns : null;

  const w = weather(e2e ? e2e.p99_typical_ns : null, budgetNs);
  $('weatherGlyph').textContent = w.glyph;
  $('weatherWord').textContent = w.word;
  $('weatherSub').textContent = w.sub;

  drawGauge(e2e ? e2e.p99_typical_ns : null, budgetNs);

  const totalLost = detail.by_service.reduce(
    (sum, s) => sum + s.samples_dropped + (s.frames_missing || 0), 0);
  $('kpis').innerHTML = [
    ['typical p50', e2e ? ns(e2e.p50_typical_ns) : '—', 'median window'],
    ['worst p99', worstP99 === null ? '—' : ns(worstP99), 'single worst window'],
    ['jitter', e2e ? ns(e2e.stddev_ns) : '—', 'pooled stddev'],
    ['in budget', pct(detail.slo_compliance_pct), `windows under ${budgetNs / 1e6} ms`],
    ['frames lost', totalLost.toLocaleString(), 'dropped + missing'],
    ['rate', detail.effective_rate_hz ? `${detail.effective_rate_hz.toFixed(1)} Hz` : '—', 'effective'],
  ].map(([label, value, note]) => `
    <div class="kpi">
      <div class="label">${label}</div>
      <div class="value">${value}</div>
      <div class="note">${note}</div>
    </div>`).join('');

  $('trophies').innerHTML = trophies(detail)
    .map(([kind, text]) => `<span class="badge ${kind}">${text}</span>`).join('');

  const banner = $('ptpBanner');
  if (!detail.ptp.trustworthy && e2e) {
    banner.classList.remove('hidden');
    banner.className = 'banner warn';
    banner.innerHTML = ptpMonitorDisabled(series)
      ? '<strong>No PTP monitor on this run</strong> — the recorder reported a disabled clock ' +
        'source for every window, which is normal for same-host runs and hosts without PTP ' +
        'hardware. Glass-to-glass and network subtract stamps taken on two machines, so they ' +
        'measure clock offset as much as latency here. Sender and processing stay within one ' +
        'host and remain valid.'
      : `<strong>Clock sync is not reliable for this session</strong> — only ` +
        `${pct(detail.ptp.reliable_pct)} of windows reported a PTP lock` +
        (detail.ptp.max_abs_offset_ns !== null
          ? `, peaking at ${ns(detail.ptp.max_abs_offset_ns)} of offset` : '') +
        '. Glass-to-glass and network are cross-host differences, so treat them as indicative ' +
        'only. Sender and processing are single-host and remain valid.';
  } else {
    banner.classList.add('hidden');
  }

  drawG2G(series, budgetNs / 1e6);
  drawBudgetSeries(series);
  drawDonut(detail.budget);
  drawHistogram(series.e2e_p50_ns);
  drawPtp(series);
  drawBarcode(series);
  fillMetrics(detail);
  fillServices(detail);
  fillShame(series);
}

/* ── wiring ─────────────────────────────────────────────────────────── */

function showError(message) {
  const box = $('error');
  box.textContent = message;
  box.classList.remove('hidden');
}

async function refreshAll() {
  $('error').classList.add('hidden');
  try {
    const first = await loadSessions();
    if (first) await loadSession(first);
  } catch (err) {
    showError(`Could not load sessions: ${err.message}`);
  }
}

$('period').addEventListener('change', () => {
  const custom = $('period').value === 'custom';
  $('customFrom').classList.toggle('hidden', !custom);
  $('customTo').classList.toggle('hidden', !custom);
  if (!custom) refreshAll();
});

$('session').addEventListener('change', async (event) => {
  $('error').classList.add('hidden');
  try {
    await loadSession(event.target.value);
  } catch (err) {
    showError(`Could not load session: ${err.message}`);
  }
});

$('slo').addEventListener('change', () => {
  if (state.detail) loadSession(state.detail.trace_id).catch((e) => showError(e.message));
});

$('reload').addEventListener('click', refreshAll);
$('fromAt').addEventListener('change', refreshAll);
$('toAt').addEventListener('change', refreshAll);

let resizeTimer;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { if (state.detail) render(); }, 150);
});

refreshAll();
