// The Matchup screen (plan P5; redesign step 5), from GET /matchup: the week's score with each
// side's projection and P(win), then two views. Overview: tonight's live against projected, the
// week day by day (final, live, projected), and the league's week so far. Players (GET
// /matchup/players, headtohead.py): your players against your opponent's, by position, best week
// first -- the week's points, or one day from the day picker drawn like the Roster's cards. The
// scores are the platform's (the server keeps them a minute), so this polls once a minute while
// the app is on screen. On a PC the Overview's league week is a column beside the rest (.two-col).
import { html, useState, useEffect } from './vendor/htm-preact-3.1.1.mjs';
import { api } from './api.js';
import { DayPicker, dayName, shortDay } from './layout.js';
import { gameLine } from './roster.js';

const store = {
  get(key) { try { return localStorage.getItem(key); } catch (_) { return null; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch (_) { /* private window */ } },
};
const STARTED = new Set(['LIVE', 'CRIT', 'OFF', 'FINAL']);
const POSITIONS = ['C', 'LW', 'RW', 'D', 'G'];
const OUT = new Set(['BN', 'IR']);

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

export function Matchup({ league, version, wide }) {
  const [data, setData] = useState(null);
  const [h2h, setH2h] = useState(null);
  const [error, setError] = useState(null);
  // The view (kept on this device; Players first, as the design) and the Players view's span.
  const [view, setView] = useState(() => store.get('fh.matchup.view') || 'players');
  const [day, setDay] = useState(null);       // null: the week; 'today' or a date: that day

  useEffect(() => {
    let gone = false;
    const load = () => Promise.all([
      api.matchup(league).then((got) => { if (!gone) { setData(got); setError(null); } }),
      api.matchupPlayers(league).then((got) => { if (!gone) setH2h(got); }).catch(() => { if (!gone) setH2h(null); }),
    ]).catch((e) => { if (!gone) setError(e); });
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
  const choose = (v) => { setView(v); store.set('fh.matchup.view', v); };
  // The day picker's days: the head-to-head's week, each with the day's score from the Overview's.
  const byDay = Object.fromEntries(data.days.map((d) => [d.day, d]));
  const week = h2h ? h2h.days : data.days.map((d) => ({ date: d.day, state: d.state === 'live' ? 'today' : d.state === 'proj' ? 'ahead' : 'final' }));
  const score = (d) => {
    const x = byDay[d.date];
    if (!x) return ['', ''];
    if (d.state === 'today') return ['live', 'you'];
    if (d.state === 'ahead') return [x.you_proj === null || x.you_proj === undefined ? '' : `${f2(x.you_proj)}–${f2(x.opp_proj)}`, 'dim'];
    return [`${f2(x.you)}–${f2(x.opp)}`, ''];
  };
  const bar = html`<div class="mu-bar">
    <div class="seg" role="group" aria-label="View">
      ${[['overview', 'Overview'], ['players', 'Players']].map(([v, label]) => html`<button type="button" aria-pressed=${view === v} onClick=${() => choose(v)}>${label}</button>`)}
    </div>
    ${view === 'players' && html`<div class="mu-span">
      <button type="button" class=${`toggle ${day === null ? 'on' : ''}`} aria-pressed=${day === null} onClick=${() => setDay(null)}>Week</button>
      <${DayPicker} week=${week} day=${day || 'today'} onDay=${setDay} active=${day !== null} onActivate=${() => setDay('today')} right=${score} />
    </div>`}
  </div>`;
  const scoreHead = html`
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
    </section>`;
  if (view === 'players') {
    return html`${scoreHead}${bar}${h2h ? html`<${HeadToHead} h2h=${h2h} day=${day} />` : html`<div class="empty">Loading…</div>`}`;
  }
  return html`${scoreHead}${bar}<div class="two-col">
    <div class="two-main">
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

// One side's player in a head-to-head row: name, eligible positions and P% (or BN), his game or
// team, and on the right his points -- the week's, or that day's expected over what he scored.
function playerCell(p, date, side) {
  if (!p) return null;
  const base = { name: p.name, elig: p.positions, prob: '', probCls: '', info: '', top: '', bot: '', dim: false };
  if (!date) {
    // Days with a lineup set (a day ahead has none: slot null).
    const counted = Object.values(p.days).filter((e) => e.counts && e.slot !== null);
    const pts = counted.reduce((t, e) => t + (e.points || 0), 0);
    return { ...base, prob: counted.length ? '' : 'BN', dim: !counted.length, info: p.team || '', top: f2(pts) };
  }
  const e = p.days[date];
  const bench = OUT.has(e.slot);
  const game = e.game;
  const out = { ...base, prob: bench ? e.slot : '', dim: bench };
  if (!game) return { ...out, info: 'No game', dim: true };
  if (STARTED.has(game.state)) {
    return { ...out, prob: bench ? e.slot : '✓', info: gameLine({}, game), top: e.xpts === null ? '' : f2(e.xpts),
      bot: e.points === null || e.points === undefined ? '' : f2(e.points), botCls: bench ? 'dim' : side };
  }
  const pp = e.p_plays === null || e.p_plays === undefined ? null : Math.round(e.p_plays * 100);
  return { ...out, prob: bench ? e.slot : pp === null ? '' : `${pp}%`, probCls: pp !== null && pp < 90 && !bench ? 'warn' : '',
    info: gameLine({}, game), top: bench || e.xpts === null ? '' : f2(e.xpts) };
}

// The Players view, each side's players paired row by row. The week: by their first position,
// best week first, each player's points this week, bench (BN) days left out. A day: that day's
// roster as the Roster's cards -- the players who play that day at the top (a game and a lineup
// slot; a day ahead has no lineup yet, so a game and not IR), the rest under Bench (the user, 2026-10-30);
// the totals add what counted (a day ahead: its projection).
function HeadToHead({ h2h, day }) {
  const entry = day === null ? null : h2h.days.find((d) => (day === 'today' ? d.state === 'today' : d.date === day));
  const date = entry ? entry.date : null;
  const shown = (side) => h2h.players.filter((p) => p.side === side && (!date || p.days[date]));
  const weekPts = (p) => Object.values(p.days).filter((e) => e.counts).reduce((t, e) => t + (e.points || 0), 0);
  // The platform's points where it has them (a day gone, today, the week so far), else the players'.
  const platform = (side) => {
    const key = side === 'me' ? 'me' : 'opp';
    const of = (x) => (x && x[key] !== null && x[key] !== undefined ? x[key] : null);
    if (date) return entry.state === 'ahead' ? of(entry.projected) : of(entry.platform);
    const gone = h2h.days.filter((d) => d.state !== 'ahead');
    return gone.every((d) => d.platform && d.platform[key] !== null && d.platform[key] !== undefined)
      ? gone.reduce((t, d) => t + d.platform[key], 0) : null;
  };
  const total = (side) => platform(side) ?? shown(side).reduce((t, p) => {
    if (!date) return t + weekPts(p);
    const e = p.days[date];
    if (!e.counts) return t;
    return t + ((entry.state === 'ahead' ? e.xpts : e.points) || 0);
  }, 0);
  const pair = (ys, os) => Array.from({ length: Math.max(ys.length, os.length) }, (_, i) => [playerCell(ys[i], date, 'you'), playerCell(os[i], date, 'opp')]);
  const rank = (p) => POSITIONS.indexOf(p.positions.split('/')[0]);
  // Plays that day: a game, and a slot that counts (a day ahead: anything but IR).
  const plays = (p) => { const e = p.days[date]; return !!e.game && e.counts; };
  const value = (p) => { const e = p.days[date]; return (entry.state === 'ahead' ? e.xpts : e.points ?? e.xpts) || 0; };
  const byDay = (list) => list.slice().sort((a, b) => rank(a) - rank(b) || value(b) - value(a));
  const groups = !date
    ? POSITIONS.map((pos) => {
      const of = (side) => shown(side).filter((p) => p.positions.split('/')[0] === pos).sort((a, b) => weekPts(b) - weekPts(a));
      return { pos, rows: pair(of('me'), of('opp')) };
    }).filter((g) => g.rows.length)
    : [{ pos: '', rows: pair(byDay(shown('me').filter(plays)), byDay(shown('opp').filter(plays))) },
      { pos: '', bench: true, rows: pair(byDay(shown('me').filter((p) => !plays(p))), byDay(shown('opp').filter((p) => !plays(p)))) }]
      .filter((g) => g.rows.length);
  const head = !date ? 'Week' : entry.state === 'today' ? 'Tonight' : shortDay(date);
  const cell = (c, right) => {
    if (!c) return html`<td></td>`;
    const pts = html`<span class="pts"><span class="xp">${c.top}</span><span class=${`lv ${c.botCls || ''}`}>${c.bot}</span></span>`;
    const who = html`<span class="who"><span class=${`nm ${c.dim ? 'dim' : ''}`}>${right
      ? html`${c.prob && html`<span class=${`pb pb-l ${c.probCls}`}>${c.prob}</span>`}<span class="el el-l">${c.elig}</span>${c.name}`
      : html`${c.name}<span class="el">${c.elig}</span>${c.prob && html`<span class=${`pb ${c.probCls}`}>${c.prob}</span>`}`}</span>
      <span class="inf">${c.info}</span></span>`;
    return html`<td><div class=${`h2h-cell ${right ? 'right' : ''}`}>${right ? html`${pts}${who}` : html`${who}${pts}`}</div></td>`;
  };
  const foot = !date
    ? 'Week points each player has scored. Bench days (BN) do not count toward the total. Players line up by position, best week first.'
    : entry.state === 'today' ? 'As on the Roster: expected points tonight, and in colour under them, the points so far. Totals add the points so far; bench (BN) points do not count.'
      : entry.state === 'final' ? `${dayName(entry, h2h.days)}: expected points, and in colour under them, the points each player scored, in the slots the platform kept. Bench (BN) points do not count.`
        : `${dayName(entry, h2h.days)}: expected points for each player with a game; no lineup is set yet. Totals are the projection of your roster as it stands, its best lineup that night, before any of the plan's moves.`;
  const note = date && entry.state === 'ahead' ? '' : " Totals are the platform's own points.";
  return html`<table class="h2h">
      <thead><tr>
        <th scope="col" class="you"><div class="hd"><span>You</span><span>${head}</span></div></th>
        <th scope="col" class="pos-col"><span class="sr">Position</span></th>
        <th scope="col" class="opp"><div class="hd"><span>${head}</span><span>Opp</span></div></th>
      </tr></thead>
      ${groups.map((g) => html`<tbody class="grp">${g.bench && html`<tr class="bench-h"><td colspan="3">Bench</td></tr>`}${g.rows.map(([y, o]) => html`<tr>
        ${cell(y, false)}<td class="pos-col">${g.pos}</td>${cell(o, true)}</tr>`)}</tbody>`)}
      <tbody><tr class="total">
        <td><div class="hd"><span>Total</span><span class="pts you">${f2(total('me'))}</span></div></td>
        <td class="pos-col"></td>
        <td><div class="hd"><span class="pts opp">${f2(total('opp'))}</span><span>Total</span></div></td>
      </tr></tbody>
    </table>
    <p class="foot">${foot}${note}</p>`;
}
