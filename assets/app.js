/* Shared rendering helpers for the Nifty 50 board (index.html) and the
   F&O sector board (sectors.html). Both pages import this file, so the tile,
   day-range, index-card and movers components cannot drift apart. */

const POLL_MS = 30000;
const STALE_MS = 20 * 60 * 1000; // flag if the data file hasn't updated in 20 min

/* Per-stock colour scale (shared by both boards so a colour means the
   same % move everywhere). */
function bucket(pct){
  if(pct === null || pct === undefined) return 'b-na';
  if(pct >= 3) return 'b-strong-gain';
  if(pct >= 2) return 'b-gain-3';
  if(pct >= 1) return 'b-gain-2';
  if(pct > 0)  return 'b-gain-1';
  if(pct === 0) return 'b-flat';
  if(pct > -1) return 'b-loss-1';
  if(pct > -2) return 'b-loss-2';
  return 'b-strong-loss';
}

/* Sector averages are far smaller in magnitude than single-stock moves, so
   they get their own finer scale. Same palette, different thresholds. */
function sectorBucket(pct){
  if(pct === null || pct === undefined) return 'b-na';
  if(pct >= 1.5)  return 'b-strong-gain';
  if(pct >= 0.75) return 'b-gain-3';
  if(pct >= 0.25) return 'b-gain-2';
  if(pct > 0)     return 'b-gain-1';
  if(pct === 0)   return 'b-flat';
  if(pct > -0.25) return 'b-loss-1';
  if(pct > -0.75) return 'b-loss-2';
  if(pct > -1.5)  return 'b-loss-3';
  return 'b-strong-loss';
}

function fmtPrice(p){
  return p === null || p === undefined ? 'N/A' : '₹' + p.toLocaleString('en-IN', {minimumFractionDigits:2, maximumFractionDigits:2});
}
function fmtPct(p){
  if(p === null || p === undefined) return '—';
  const sign = p >= 0 ? '+' : '';
  return sign + p.toFixed(2) + '%';
}
function fmtCompact(p){
  return p === null || p === undefined ? '—' : p.toLocaleString('en-IN', { maximumFractionDigits: 0 });
}
function nseUrl(ticker){
  const symbol = ticker.replace(/\.NS$/, '');
  return `https://www.nseindia.com/get-quotes/equity?symbol=${encodeURIComponent(symbol)}`;
}
function slug(s){
  return s.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
}

function dayRangeBar(r){
  if(r.dayLow === null || r.dayLow === undefined || r.dayHigh === null || r.dayHigh === undefined || r.dayHigh <= r.dayLow || r.price === null){
    return '';
  }
  let pos = (r.price - r.dayLow) / (r.dayHigh - r.dayLow) * 100;
  pos = Math.min(100, Math.max(0, pos));
  return `
    <div class="day-range">
      <div class="track"><div class="dot" style="left:${pos.toFixed(1)}%"></div></div>
      <div class="labels"><span>${fmtCompact(r.dayLow)}</span><span>${fmtCompact(r.dayHigh)}</span></div>
    </div>
  `;
}

/* A name trading ex a corporate action. The raw print is an artefact - the
   share count or the company changed overnight - so the tile is LABELLED
   rather than left to read as a catastrophic loss. A split or bonus has an
   exact ratio from its announced terms, so the % shown is the real
   like-for-like move; a demerger has no such ratio, so it reads NA. */
function caTag(r){
  if(!r.ca) return '';
  const what = /split/i.test(r.ca.what) ? 'ex-split'
             : /bonus/i.test(r.ca.what) ? 'ex-bonus' : 'ex-demerger';
  return `<div class="ca-tag${r.ca.adjusted ? '' : ' na'}">${what}${r.ca.adjusted ? ' · adj' : ''}</div>`;
}
function caTitle(r){
  if(!r.ca) return '';
  return ` · Ex ${r.ca.what} (${r.ca.date}). Raw print ${fmtPct(r.ca.rawPct)} is an artefact; `
    + (r.ca.adjusted
        ? 'shown adjusted for the announced ratio, which is the real move.'
        : 'a demerger has no like-for-like change, so this reads NA today.');
}

/* One stock tile: name, price, % change, day-range bar, click through to NSE. */
function tileHtml(r){
  const pctText = (r.ca && !r.ca.adjusted) ? 'NA' : fmtPct(r.pct);
  return `
    <a class="tile ${bucket(r.pct)}${r.cashOnly ? ' cash-only' : ''}${r.ca ? ' ex-ca' : ''}" href="${nseUrl(r.ticker)}" target="_blank" rel="noopener noreferrer" title="Day range: ${fmtPrice(r.dayLow)} – ${fmtPrice(r.dayHigh)}${r.cashOnly ? ' · cash only, no F&O' : ''}${caTitle(r)} · View on NSE">
      <div class="name">${r.name}${r.ca ? '<span class="adj-star" title="price history adjusted for a corporate action">*</span>' : ''}</div>
      <div class="figures">
        <div class="price">${fmtPrice(r.price)}</div>
        <div class="pct">${pctText}</div>
      </div>
      ${caTag(r)}
      ${dayRangeBar(r)}
    </a>
  `;
}

function indexCardHtml(displayName, idx){
  if(!idx || idx.price === undefined || idx.price === null){
    return `<div class="index-card"><div class="index-card-head"><span class="idx-name">${displayName}</span></div><span style="color:var(--text-dim);font-size:12px;">No data</span></div>`;
  }
  const up = idx.pct >= 0;
  const headHtml = `
    <div class="index-card-head">
      <span class="idx-name">${displayName}</span>
      <span class="idx-figures">
        <span class="idx-price">${idx.price.toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2})}</span>
        <span class="idx-delta ${up ? 'up':'down'}">${up?'+':''}${idx.pct.toFixed(2)}% (${up?'+':''}${idx.pts.toFixed(1)} pts)</span>
      </span>
    </div>
  `;

  let rangeHtml = '';
  if(idx.dayLow !== null && idx.dayLow !== undefined && idx.dayHigh !== null && idx.dayHigh !== undefined && idx.dayHigh > idx.dayLow){
    let pos = (idx.price - idx.dayLow) / (idx.dayHigh - idx.dayLow);
    pos = Math.min(0.94, Math.max(0.06, pos));
    const leftExpr = `calc(44px + (100% - 88px) * ${pos.toFixed(4)})`;
    rangeHtml = `
      <div class="range-visual">
        <div class="track-line"></div>
        <div class="end-pill low">${idx.dayLow.toLocaleString('en-IN',{maximumFractionDigits:0})}</div>
        <div class="end-pill high">${idx.dayHigh.toLocaleString('en-IN',{maximumFractionDigits:0})}</div>
        <div class="cur-stem" style="left:${leftExpr}"></div>
        <div class="cur-pill" style="left:${leftExpr}">${idx.price.toLocaleString('en-IN',{maximumFractionDigits:2})}</div>
        <div class="cur-dot" style="left:${leftExpr}"></div>
      </div>
    `;
  }
  return `<div class="index-card">${headHtml}${rangeHtml}</div>`;
}

function moversList(items, field){
  return items.map(r => `
    <div class="mover-row">
      <div class="mover-top">
        <span class="m-name">${r.name}</span>
        <span class="m-price">${fmtPrice(r.price)}</span>
        <span class="m-pct">${fmtPct(r[field])}</span>
      </div>
      ${dayRangeBar(r)}
    </div>
  `).join('');
}

function renderLegend(elId){
  const stops = [
    ['b-strong-loss','≤ -2%'], ['b-loss-2','-1.5%'], ['b-loss-1','-0.5%'],
    ['b-flat','0%'], ['b-gain-1','+0.5%'], ['b-gain-2','+1.5%'], ['b-strong-gain','≥ +3%'],
  ];
  const el = document.getElementById(elId);
  if(el) el.innerHTML = stops.map(([cls,lbl]) =>
    `<div class="chip ${cls}"></div><span class="lbl">${lbl}</span>`
  ).join('');
}

function setStatus(text, stale){
  const t = document.getElementById('statusText');
  const tag = document.getElementById('statusTag');
  if(t) t.textContent = text;
  if(tag) tag.classList.toggle('stale', !!stale);
}

/* Poll `dataFile` every POLL_MS and hand the parsed payload to `renderFn`. */
function startPolling(dataFile, renderFn){
  async function poll(){
    try{
      const resp = await fetch(dataFile + '?t=' + Date.now(), { cache: 'no-store' });
      if(!resp.ok) throw new Error('HTTP ' + resp.status);
      const data = await resp.json();
      renderFn(data);

      const generated = new Date(data.generatedAt);
      const ageMs = Date.now() - generated.getTime();
      const ageMin = Math.round(ageMs / 60000);
      const timeStr = generated.toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', second: '2-digit' });
      const stale = ageMs > STALE_MS;
      setStatus(`Updated ${timeStr} IST · ${ageMin < 1 ? 'just now' : ageMin + 'm ago'}${stale ? ' (stale)' : ''}`, stale);
    }catch(e){
      setStatus(`Could not reach ${dataFile} — retrying…`, true);
    }
  }
  poll();
  setInterval(poll, POLL_MS);
}

/* Floating "back to top" button. Created from here so both boards get it and
   neither can drift. Appears once you are past the first screenful, sits clear
   of the left-aligned sector headings, and honours reduced-motion. */
function initBackToTop(showAfter = 400){
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'to-top';
  btn.setAttribute('aria-label', 'Back to top');
  btn.title = 'Back to top';
  btn.innerHTML = '<span aria-hidden="true">&#8593;</span> Top';
  document.body.appendChild(btn);

  btn.addEventListener('click', () => {
    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    window.scrollTo({ top: 0, behavior: reduce ? 'auto' : 'smooth' });
  });

  let shown = null;
  function sync(){
    const show = window.scrollY > showAfter;
    if(show !== shown){
      shown = show;
      btn.classList.toggle('visible', show);
    }
  }
  sync();
  window.addEventListener('scroll', sync, { passive: true });
  window.addEventListener('resize', sync, { passive: true });

  // The board renders after its data arrives. If the browser restored a scroll
  // position on reload, that happens while the document is still short and no
  // scroll event follows - so re-check whenever the page height changes.
  if(typeof ResizeObserver !== 'undefined'){
    new ResizeObserver(sync).observe(document.body);
  }
}

/* Shown only while a name is actually trading ex a corporate action, so the
   board carries no standing caveat on the ~364 days a year when none is. */
function renderCaNote(rows){
  const el = document.getElementById('caNote');
  if(!el) return;
  const hits = (rows || []).filter(r => r && r.ca);
  if(!hits.length){ el.hidden = true; el.innerHTML = ''; return; }
  el.hidden = false;
  el.innerHTML = '<strong>Trading ex a corporate action today.</strong> '
    + hits.map(r => {
        const base = `<strong>${r.name}</strong> — ex ${r.ca.what} (${r.ca.date}), `
          + `raw print ${fmtPct(r.ca.rawPct)}`;
        return base + (r.ca.adjusted
          ? `, shown adjusted to <strong>${fmtPct(r.pct)}</strong> using the announced ratio.`
          : `, shown as <strong>NA</strong>: a demerger changes the company itself, so there`
            + ` is no like-for-like change to quote, and the name is left out of its sector average.`);
      }).join(' ')
    + ' The raw figure is an artefact of the share count or the company changing overnight, not a move. '
    + 'Large moves that are <em>not</em> a listed corporate action are never rewritten.';
}
