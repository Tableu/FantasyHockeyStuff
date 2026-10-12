// The Matchup screen (plan P5), from GET /matchup: the week's score with each side's projection and
// P(win), tonight's live against projected, the week day by day (final, live, projected), and the
// league's week so far. The scores are the platform's (the server keeps them a minute), so this
// polls once a minute while the app is on screen. On a PC the league's week is a column beside the
// rest (app.css, .two-col).
import { html, useState, useEffect } from './vendor/htm-preact-3.1.1.mjs';
import { api } from './api.js';

const EVERY_MS = 60000;
const f2 = (x) => (x === null || x === undefined ? '—' : Number(x).toFixed(2));
const sg = (x) => (x > 0 ? '+' : x < 0 ? '−' : '') + Math.abs(x).toFixed(2);

function dayLabel(iso, withMonth = true) {
  const [y, m, d] = iso.split('-').map(Number);
  const t = new Date(y, m - 1, d);
  const wd = t.toLocaleDateString('en-US', { weekday: 'short' });
  return withMonth ? `${wd} ${t.toLocaleDateString('en-US', { month: 'short' })} ${d}` : wd;
}
const ord = (n) => n + (n % 10 === 1 && n % 100 !== 11 ? 'st' : n % 10 === 2 && n % 100 !== 12 ? 'nd' : n % 10 === 3 && n % 100 !== 13 ? 'rd' : 'th');

function counts(side) {
  if (side.to_play === null || side.to_play === undefined) return null;
  return `${side.in_play} playing · ${side.to_play} to come · ${side.done} done`;
}

export function Matchup({ league, version }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let gone = false;
    const load = () => api.matchup(league)
      .then((got) => { if (!gone) { setData(got); setError(null); } })
      .catch((e) => { if (!gone) setError(e); });
    load();
    const timer = setInterval(() => { if (document.visibilityState === 'visible') load(); }, EVERY_MS);
    const onShow = () => { if (document.visibilityState === 'visible') load(); };
    document.addEventListener('visibilitychange', onShow);
    return () => { gone = true; clearInterval(timer); document.removeEventListener('visibilitychange', onShow); };
  }, [league, version]);

  if (error && !data) return html`<div class=${error.status === 409 ? 'empty' : 'error'} role="alert">${error.message}</div>`;
  if (!data) return html`<div class="empty">Loading…</div>`;
  if (!data.available) return html`<div class="empty">${data.reason}</div>`;
  const { you, opp } = data;
  const lead = you.points - (opp ? opp.points : 0);
  const margin = data.projected ? data.projected.you - data.projected.opp : null;
  const pwin = data.p_win === null || data.p_win === undefined ? null : Math.round(data.p_win * 100);
  const rank = data.league.findIndex((t) => t.who === 'you') + 1;
  const daysLeft = data.days_left;
  return html`<div class="two-col">
    <div class="two-main">
    ${error && html`<div class="error" role="alert">${error.message}</div>`}
    <section class="week-score" aria-label="Week score">
      <div class="ws-top"><span>Week ${data.period} · ${dayLabel(data.first_day)}–${dayLabel(data.last_day).split(' ').slice(1).join(' ')}</span>
        <span>${daysLeft === 0 ? 'last day' : daysLeft === 1 ? '1 day left' : `${daysLeft} days left`}</span></div>
      <div class="ws-grid">
        <div class="ws-side"><span class="nm you">${you.name}</span><span class="pts mono">${f2(you.points)}</span>
          ${data.projected && html`<span class="proj mono">proj ${f2(data.projected.you)}</span>`}</div>
        <span class="vs">vs</span>
        <div class="ws-side right"><span class="nm opp">${opp ? opp.name : 'No opponent'}</span><span class="pts mono">${opp ? f2(opp.points) : ''}</span>
          ${data.projected && html`<span class="proj mono">proj ${f2(data.projected.opp)}</span>`}</div>
      </div>
      ${pwin !== null && html`<div class="ws-win">
        <div class="ws-bar" role="img" aria-label=${`Chance to win ${pwin}%`}><span style=${`width: ${pwin}%`}></span><span></span></div>
        <div class="ws-foot"><span>P(win) <b class="mono you">${pwin}%</b></span>
          <span class="dim">lead ${sg(lead)}${margin !== null ? ` · proj margin ${sg(margin)}` : ''}</span></div>
      </div>`}
      ${pwin === null && html`<div class="ws-foot"><span class="dim">lead ${sg(lead)}</span><span class="dim">No P(win) yet: refresh for this week's plan.</span></div>`}
    </section>

    ${data.today && opp && html`
      <div class="sec"><h2>Tonight</h2><span class="note">${dayLabel(data.today)} · live</span></div>
      <div class="tonight">
        ${[['You', you, 'you'], ['Opponent', opp, 'opp']].map(([label, s, cls]) => html`<div>
          <span class="k">${label} · live / proj</span>
          <span class="v mono"><span class=${cls}>${f2(s.live)}</span><span class="dim"> / ${f2(s.tonight_proj)}</span></span>
          ${counts(s) && html`<span class="c">${counts(s)}</span>`}
        </div>`)}
      </div>`}

    <div class="sec"><h2>Week by day</h2><span class="note">final · <span style="color: var(--text)">live</span> · projected</span></div>
    <table class="list fixed byday">
      <thead><tr><th scope="col">Day</th><th scope="col" class="n you">You</th><th scope="col" class="n opp">Opp</th>
        <th scope="col" class="n" style="width: 58px">+/−</th><th scope="col" class="n" style="width: 74px">Running</th></tr></thead>
      <tbody>${data.days.map((d) => {
        const live = d.state === 'live', proj = d.state === 'proj';
        const cell = (v, p) => (proj ? (p === null || p === undefined ? '—' : f2(p)) : live && p !== null && p !== undefined ? `${f2(v)} (${f2(p)})` : f2(v));
        const diff = proj ? null : d.you - d.opp;
        return html`<tr class=${live ? 'live-row' : proj ? 'proj-row' : ''}>
          <td>${dayLabel(d.day)}${live ? html` <span class="meta">live</span>` : proj ? html` <span class="meta">proj</span>` : ''}</td>
          <td class=${`n ${live ? 'you wrap' : ''}`}>${cell(d.you, d.you_proj)}</td>
          <td class=${`n ${live ? 'opp wrap' : ''}`}>${cell(d.opp, d.opp_proj)}</td>
          <td class=${`n ${diff === null ? '' : diff >= 0 ? 'you' : 'opp'}`}>${diff === null ? '' : sg(diff)}</td>
          <td class=${`n ${d.run === null ? '' : d.run >= 0 ? 'you' : 'opp'}`}>${d.run === null ? '' : sg(d.run)}</td>
        </tr>`;
      })}</tbody>
    </table>
    ${!data.has_projection && html`<p class="foot">Projected days fill in once a refresh makes this week's plan with its projection.</p>`}
    </div>
    <aside class="two-side">

    <div class="sec"><h2>League this week</h2><span class="note">You're <b style="color: var(--text)">${ord(rank)}</b> of ${data.league.length}</span></div>
    <table class="list fixed">
      <thead><tr><th scope="col" style="width: 40px">#</th><th scope="col">Team</th><th scope="col" class="n" style="width: 64px">Pts</th></tr></thead>
      <tbody>${data.league.map((t, i) => html`<tr class=${t.who ? `tint-${t.who === 'you' ? 'me' : 'opp'}` : ''}>
        <td class="n dim" style="text-align: left">${i + 1}</td>
        <td class=${t.who === 'you' ? 'you strong' : t.who === 'opp' ? 'opp strong' : ''}>${t.name}</td>
        <td class=${`n ${t.who ? 'strong' : ''}`}>${f2(t.points)}</td></tr>`)}</tbody>
    </table>
    <p class="foot">Week points so far, tonight's live points included, from the platform (read at most once a minute).</p>
    </aside>
  </div>`;
}
