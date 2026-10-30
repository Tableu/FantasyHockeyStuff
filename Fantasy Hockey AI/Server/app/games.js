// Games (plan P4): today's games from GET /games with the goal feed (GET /goals) in a sheet at the
// bottom; a game opens full screen (#/game/<id>/<view>) with its Summary, Box, Plays (GET
// /games/<id>) and Lines (GET /games/<id>/lines). Live, they poll every 5 s -- the server's feed
// cache (about 20 s) is what bounds the calls to the NHL. On a PC (`wide`) the list, the chosen
// game and the goal feed sit side by side: the game opens in the middle, the feed is a rail.
import { html, useState, useEffect, useRef } from './vendor/htm-preact-3.1.1.mjs';
import { api } from './api.js';
import { useEscape, Photo } from './layout.js';

const LIVE = new Set(['LIVE', 'CRIT']);
const DONE = new Set(['OFF', 'FINAL']);
const LIVE_EVERY_MS = 5000;
const IDLE_EVERY_MS = 60000;
const ORD = { 1: '1st', 2: '2nd', 3: '3rd' };
const f2 = (x) => (x === null || x === undefined ? '' : Number(x).toFixed(2));
const signed = (x) => (x > 0 ? '+' : x < 0 ? '−' : '') + Math.abs(x).toFixed(2);

const store = {
  get(key) { try { return localStorage.getItem(key); } catch (_) { return null; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch (_) { /* private window */ } },
};

function periodName(number, type) {
  return type === 'OT' ? 'OT' : type === 'SO' ? 'SO' : ORD[number] || `P${number}`;
}

function clockTime(iso) {
  return new Date(iso).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
}

// A live game's man advantage (the server's situation): a red "PP" (or "Pulled", for a pulled
// goalie) beside the team that has it, on a card and in the game's header. Even strength: none.
function SituationTag({ s, team }) {
  if (!s || s.team !== team) return null;
  return html`<span class=${`sit ${s.kind}`}>${s.kind === 'pulled' ? 'Pulled' : 'PP'}</span>`;
}

// ...and, in the game's header, its skater count under the period and clock: "5v4 · 1:42" (the
// power play's time left). Cards leave it out.
function SituationLine({ s }) {
  if (!s) return null;
  const left = s.kind === 'pp' && s.left ? s.left.replace(/^0(?=\d:)/, '') : '';
  return html`<span class="sit-line">${s.skaters[0]}v${s.skaters[1]}${left ? ` · ${left}` : ''}</span>`;
}

// "2nd 14:21", "INT · end 1st", "Final", "Final/OT", or the start time.
export function stateLabel(g) {
  if (DONE.has(g.state)) return g.period_type && g.period_type !== 'REG' ? `Final/${g.period_type}` : 'Final';
  if (LIVE.has(g.state)) {
    if (g.intermission) return `INT · end ${periodName(g.period, g.period_type)}`;
    return `${periodName(g.period, g.period_type)} ${g.clock || ''}`.trim();
  }
  return clockTime(g.start_utc);
}

function todayLabel() {
  const t = new Date();
  return `${t.toLocaleDateString('en-US', { weekday: 'short' })} ${t.toLocaleDateString('en-US', { month: 'short' })} ${t.getDate()}`;
}

// Polls `load` every 5 s while `live()` says a game is on, else every minute; at once when the
// app comes back on screen.
function usePoll(load, deps, live) {
  useEffect(() => {
    let gone = false, timer = null;
    const tick = async () => {
      let data = null;
      try { data = await load(); } catch (_) { /* the screen shows the error */ }
      if (!gone) timer = setTimeout(tick, live(data) ? LIVE_EVERY_MS : IDLE_EVERY_MS);
    };
    const onShow = () => { if (document.visibilityState === 'visible') { clearTimeout(timer); tick(); } };
    tick();
    document.addEventListener('visibilitychange', onShow);
    return () => { gone = true; clearTimeout(timer); document.removeEventListener('visibilitychange', onShow); };
  }, deps);
}

// A team's logo: the NHL's own, its dark-background version (assets.nhle.com, as the score feed
// links them; the service worker keeps them for offline). The abbreviation in a circle if it
// cannot load.
const logoUrl = (abbr) => `https://assets.nhle.com/logos/nhl/svg/${abbr}_dark.svg`;

function Logo({ abbr, big }) {
  const [failed, setFailed] = useState(false);
  if (failed || !abbr) {
    return html`<span class=${`logo ${big ? 'big' : ''}`} role="img" aria-label=${`${abbr} logo`}>${abbr}</span>`;
  }
  return html`<img class=${`logo-img ${big ? 'big' : ''}`} src=${logoUrl(abbr)} alt=${`${abbr} logo`}
    loading="lazy" decoding="async" onError=${() => setFailed(true)} />`;
}

// ---------- the games list and the goal feed ----------

const FEED_TOP = 96;          // the open feed's top, below the screen's head (.feed.open)
const FEED_SNAP = 0.3;        // a drag past this share of the way opens (or closes) the feed

// The feed's bar dragged up or down: the sheet follows the finger and, let go, settles open or
// closed -- past FEED_SNAP of the way, or flicked that way. A tap without a drag still toggles.
function useFeedDrag(open, onToggle) {
  const sheet = useRef(null);
  const [height, setHeight] = useState(null);     // px while dragging or settling, else null
  const [settling, setSettling] = useState(false);
  const drag = useRef(null);
  const moved = useRef(false);
  const bounds = () => {
    const el = sheet.current, box = el.offsetParent.getBoundingClientRect(), rect = el.getBoundingClientRect();
    return { max: rect.bottom - box.top - FEED_TOP, min: 96 };
  };
  const down = (e) => {
    if (e.button > 0 || !sheet.current) return;
    const { min, max } = bounds();
    drag.current = { y: e.clientY, start: open ? max : Math.min(sheet.current.offsetHeight, min), min, max, t: e.timeStamp, v: 0, last: e.clientY };
    moved.current = false;
    e.currentTarget.setPointerCapture(e.pointerId);
  };
  const move = (e) => {
    const d = drag.current;
    if (!d) return;
    const dy = e.clientY - d.y;
    if (!moved.current && Math.abs(dy) < 6) return;
    setSettling(false);
    moved.current = true;
    const dt = Math.max(1, e.timeStamp - d.t);
    d.v = (e.clientY - d.last) / dt;              // px per ms, + = down
    d.t = e.timeStamp; d.last = e.clientY;
    setHeight(Math.max(d.min, Math.min(d.max, d.start - dy)));
  };
  const up = (e) => {
    const d = drag.current;
    drag.current = null;
    if (!d || !moved.current) return;
    const h = Math.max(d.min, Math.min(d.max, d.start - (e.clientY - d.y)));
    const share = (h - d.min) / Math.max(1, d.max - d.min);
    const flick = Math.abs(d.v) > 0.5 ? (d.v < 0 ? 'open' : 'closed') : null;
    const toOpen = flick ? flick === 'open' : open ? share > 1 - FEED_SNAP : share > FEED_SNAP;
    setSettling(true);
    setHeight(toOpen ? d.max : d.min);            // settles there (the transition), then the class takes over
    setTimeout(() => { setHeight(null); setSettling(false); if (toOpen !== open) onToggle(); }, 180);
  };
  // The click after a drag is the drag's own, not a tap.
  const click = (e) => { if (moved.current) { moved.current = false; e.preventDefault(); return; } onToggle(); };
  return { sheet, height, settling, handlers: { onPointerDown: down, onPointerMove: move, onPointerUp: up, onPointerCancel: up, onClick: click } };
}

function GoalFeed({ goals, open: opened, onToggle, rail }) {
  const [filter, setFilter] = useState('all');
  const { sheet, height, settling, handlers } = useFeedDrag(!!opened, onToggle);
  const dragging = !rail && height !== null;
  const open = rail || opened || dragging;
  useEscape(!rail && opened, onToggle);
  if (!goals) return null;
  const rows = goals.goals.filter((g) => filter === 'all' || g.scorer.owner || g.assists.some((a) => a.owner));
  const fp = (g) => {
    const by = { me: 0, opp: 0 };
    for (const p of [g.scorer, ...g.assists]) if (p.owner) by[p.owner] += p.points;
    return by;
  };
  return html`
    ${open && !rail && html`<div class="feed-scrim" onClick=${onToggle}></div>`}
    <section ref=${sheet} class=${`feed ${open ? 'open' : ''} ${rail ? 'rail' : ''} ${dragging ? 'dragging' : ''} ${settling ? 'settling' : ''}`}
      style=${dragging ? `top: auto; height: ${height}px` : ''} aria-label="Goal feed">
      <div class="feed-bar">
        ${rail ? html`<div class="feed-toggle">
          <span class="feed-title">Goals · FP <span class="you">You ${f2(goals.totals.me)}</span> · <span class="opp">Opp ${f2(goals.totals.opp)}</span></span>
        </div>` : html`<button type="button" class="feed-toggle" aria-expanded=${!!opened} ...${handlers}
          aria-label=${opened ? 'Close the goal feed' : 'Open the goal feed (or drag it up)'}>
          <span class="grip"></span>
          <span class="feed-title">Goals · FP <span class="you">You ${f2(goals.totals.me)}</span> · <span class="opp">Opp ${f2(goals.totals.opp)}</span>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" style=${`margin-left: auto; transform: rotate(${opened ? 180 : 0}deg)`}><path d="m6 15 6-6 6 6"></path></svg></span>
        </button>`}
        ${open && html`<div class="seg" role="group" aria-label="Filter goals">
          <button type="button" aria-pressed=${filter === 'all'} onClick=${() => setFilter('all')}>All</button>
          <button type="button" aria-pressed=${filter === 'fantasy'} onClick=${() => setFilter('fantasy')}>Fantasy</button>
        </div>`}
      </div>
      <div class="feed-body">
        ${!rows.length ? html`<p class="foot">${goals.goals.length ? 'No fantasy goals yet.' : 'No goals yet today.'}</p>` : html`
        <table class="list fixed">
          <colgroup><col style="width: 48px" /><col /><col style="width: 30px" /><col style="width: 58px" /><col style="width: 42px" /></colgroup>
          ${open && html`<thead><tr><th scope="col">Tm</th>
            <th scope="col">Scorer · assists</th><th scope="col">Str</th>
            <th scope="col" class="n">FP</th><th scope="col"><span class="sr">Clip</span></th></tr></thead>`}
          <tbody>${(open ? rows : rows.slice(0, 1)).map((g) => {
            const by = fp(g);
            const side = by.me ? 'me' : by.opp ? 'opp' : '';
            return html`<tr class=${side ? `tint-${side}` : ''}>
              <td><b>${g.team}</b></td>
              <td class="who"><div class="who-row"><${Photo} src=${g.scorer.photo} name=${g.scorer.name} size=${36} />
                <div class="who-text"><span class="scorer">${g.scorer.name} <span class="meta">(${g.scorer.to_date})</span></span>
                ${g.assists.map((a) => html`<span class="meta assists">${a.name}</span>`)}</div></div></td>
              <td class="str">${g.strength}</td>
              <td class=${`n ${by.me && by.opp ? 'fp-both' : ''}`}>${by.me ? html`<span class="you">${signed(by.me)}</span>` : ''}${by.me && by.opp ? ' ' : ''}${by.opp ? html`<span class="opp">${signed(by.opp)}</span>` : ''}</td>
              <td>${g.clip && html`<a class="clip" href=${g.clip} target="_blank" rel="noopener" aria-label=${`Highlight: ${g.scorer.name}`}>
                <svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M7 4v16l13-8z"></path></svg></a>`}</td>
            </tr>`;
          })}</tbody>
        </table>
        ${open && html`<p class="foot">Rows tinted <span class="you">blue</span> are your points, <span class="opp">orange</span> your opponent's.</p>`}`}
      </div>
    </section>`;
}

function card(g, href, current) {
  const done = DONE.has(g.state), started = done || LIVE.has(g.state);
  const lead = started && g.away.score !== g.home.score ? (g.away.score > g.home.score ? 'away' : 'home') : null;
  return html`<a class="game-card" href=${href} aria-current=${current ? 'page' : undefined}
    aria-label=${`${g.away.abbrev} at ${g.home.abbrev}, ${stateLabel(g)}`}>
    <div class="game-top">
      <span class=${`state ${LIVE.has(g.state) ? 'live' : ''}`}>${LIVE.has(g.state) && !g.intermission && html`<span class="dot"></span>`}${stateLabel(g)}</span>
    </div>
    ${['away', 'home'].map((side) => html`<div class="game-team">
      <${Logo} abbr=${g[side].abbrev} />
      <span class="tn"><span class="ab"><span class=${done && lead && lead !== side ? 'dim2' : ''}>${g[side].abbrev}</span><${SituationTag} s=${g.situation} team=${g[side].abbrev} /></span>
        ${started && g[side].sog !== null && html`<span class="sog">SOG: ${g[side].sog}</span>`}</span>
      <span class=${`score ${done && lead && lead !== side ? 'dim2' : ''}`}>${started ? g[side].score : ''}</span>
    </div>`)}
  </a>`;
}

export function Games({ league, version, wide, selected, view }) {
  const [games, setGames] = useState(null);
  const [goals, setGoals] = useState(null);
  const [error, setError] = useState(null);
  const [open, setOpen] = useState(false);

  usePoll(async () => {
    try {
      const [g, gl] = await Promise.all([api.games(league), api.goals(league)]);
      setGames(g); setGoals(gl); setError(null);
      return g;
    } catch (e) {
      setError(e.message);
      throw e;
    }
  }, [league, version], (g) => !!g && g.games.some((x) => LIVE.has(x.state)));

  const live = games && games.games.some((x) => LIVE.has(x.state));
  if (wide) {
    // A game card keeps the view the open game shows, so moving between games stays on Box (say).
    return html`<div class="games-wide">
      <section class="games-list" aria-label="Today's games">
        <header class="screen-head">
          <span class="h">Games <span class="dim">${todayLabel()}</span></span>
          ${live && html`<span class="live-tag"><span class="dot"></span>Live · 5s</span>`}
        </header>
        ${error && html`<div class="error" role="alert">${error}</div>`}
        ${!games ? html`<div class="empty">Loading…</div>` : !games.games.length ? html`<div class="empty">No NHL games today.</div>`
          : html`<div class="game-grid">${games.games.map((g) => card(g, `#/game/${g.id}/${view || 'summary'}`, g.id === selected))}</div>`}
      </section>
      <section class="game-pane" aria-label="Game">
        ${selected ? html`<${Game} key=${selected} league=${league} gameId=${selected} view=${view} embedded />`
          : html`<div class="empty">Choose a game for its summary, box score, plays and lines.</div>`}
      </section>
      <${GoalFeed} goals=${goals} rail />
    </div>`;
  }
  // The phone: the app's header (the team selector) is above it, not a Games bar of its own.
  return html`
    <main class="screen games-screen">
      ${error && html`<div class="error" role="alert">${error}</div>`}
      ${!games ? html`<div class="empty">Loading…</div>` : !games.games.length ? html`<div class="empty">No NHL games today.</div>` : html`
      <div class="game-grid">
        ${games.games.map((g) => card(g, `#/game/${g.id}/summary`, false))}
      </div>
      <p class="foot">Tap a game for its summary.</p>`}
    </main>
    <${GoalFeed} goals=${goals} open=${open} onToggle=${() => setOpen(!open)} />`;
}

// ---------- one game ----------

const STAT_ROWS = [
  ['sog', 'Shots on goal'], ['hits', 'Hits'], ['blockedShots', 'Blocked shots'], ['faceoffWins', 'Faceoff wins'],
  ['giveaways', 'Giveaways', true], ['takeaways', 'Takeaways'], ['powerPlay', 'Power play'], ['pim', 'Penalty minutes', true],
];

function Summary({ game }) {
  const stats = Object.fromEntries((game.team_stats || []).map((s) => [s.category, s]));
  const num = (v) => (typeof v === 'string' && v.includes('/') ? Number(v.split('/')[0]) : Number(v));
  const rows = STAT_ROWS.filter(([key]) => stats[key]).map(([key, label, lowerBetter]) => {
    const s = stats[key];
    let a = num(s.awayValue), h = num(s.homeValue);
    let aText = String(s.awayValue), hText = String(s.homeValue);
    if (key === 'faceoffWins' && stats.faceoffWinningPctg) {
      aText = `${a} · ${Math.round(stats.faceoffWinningPctg.awayValue * 100)}%`;
      hText = `${Math.round(stats.faceoffWinningPctg.homeValue * 100)}% · ${h}`;
    }
    if (key === 'powerPlay' && stats.powerPlayPctg) {
      aText = `${s.awayValue} · ${Math.round(stats.powerPlayPctg.awayValue * 100)}%`;
      hText = `${Math.round(stats.powerPlayPctg.homeValue * 100)}% · ${s.homeValue}`;
      a = stats.powerPlayPctg.awayValue; h = stats.powerPlayPctg.homeValue;
    }
    const total = a + h || 1;
    const aLead = lowerBetter ? a < h : a > h, hLead = lowerBetter ? h < a : h > a;
    return { label, aText, hText, aLead, hLead, share: `${Math.round((a / total) * 100)}%` };
  });
  const periods = game.linescore || [];
  const shots = Object.fromEntries((game.shots_by_period || []).map((p) => [p.periodDescriptor.number, p]));
  return html`
    ${periods.length > 0 && html`<table class="list periods">
      <thead><tr><th scope="col">Goals (shots)</th>${periods.map((p) => html`<th scope="col" class="n">${periodName(p.periodDescriptor.number, p.periodDescriptor.periodType)}</th>`)}<th scope="col" class="n">T</th></tr></thead>
      <tbody>${['away', 'home'].map((side) => html`<tr><th scope="row">${game[side].abbrev}</th>
        ${periods.map((p) => html`<td class="n">${p[side]} <span class="meta">(${(shots[p.periodDescriptor.number] || {})[side] ?? ''})</span></td>`)}
        <td class="n"><b>${game[side].score}</b> <span class="meta">(${game[side].sog})</span></td></tr>`)}</tbody>
    </table>`}
    <table class="list team-stats">
      <thead><tr><th scope="col">${game.away.abbrev}</th><th scope="col" style="text-align: center">Team stats</th><th scope="col" style="text-align: right">${game.home.abbrev}</th></tr></thead>
      <tbody>${rows.map((r) => html`<tr>
        <td class=${`n ${r.aLead ? 'lead' : 'dim2'}`} style="text-align: left">${r.aText}</td>
        <th scope="row"><span>${r.label}</span><span class="bar" aria-hidden="true"><span class=${r.aLead ? 'hi' : ''} style=${`width: ${r.share}`}></span><span class=${r.hLead ? 'hi' : ''} style="flex: 1"></span></span></th>
        <td class=${`n ${r.hLead ? 'lead' : 'dim2'}`}>${r.hText}</td>
      </tr>`)}</tbody>
    </table>
    <p class="foot">The higher number in each row is bold; for giveaways and penalty minutes, the lower. Power play = goals / chances.${LIVE.has(game.state) ? ' Updates every 5 s while the game is live.' : ''}</p>`;
}

// FP first, so it stays on screen while the rest scroll sideways on a phone. PP / SH TOI come from
// the shift reports (as Lines): blank until they are posted.
const SKATER_COLS = [['fantasy', 'FP'], ['goals', 'G'], ['assists', 'A'], ['shots', 'SOG'], ['hits', 'HIT'],
  ['blocks', 'BLK'], ['pim', 'PIM'], ['faceoff_wins', 'FOW'], ['faceoff_pct', 'FO%'], ['toi', 'TOI'],
  ['pp_toi', 'PP TOI'], ['sh_toi', 'SH TOI']];
const TOI_KEYS = new Set(['toi', 'pp_toi', 'sh_toi']);
const toiSeconds = (t) => (t ? t.split(':').reduce((m, s) => m * 60 + Number(s), 0) : 0);
function skaterCell(p, key) {
  if (key === 'fantasy') return f2(p.fantasy);
  if (key === 'faceoff_pct') return p.faceoff_pct == null ? '' : Math.round(p.faceoff_pct * 100);
  return p[key] ?? '';
}

function Box({ game, league }) {
  const [side, setSide] = useState('home');
  const [sort, setSort] = useState({ key: 'fantasy', desc: true });
  const team = game[side];
  const value = (p, key) => (TOI_KEYS.has(key) ? toiSeconds(p[key]) : p[key] ?? -1);
  const skaters = team.players.filter((p) => p.position !== 'G')
    .sort((a, b) => (value(b, sort.key) - value(a, sort.key)) * (sort.desc ? 1 : -1) || a.name.localeCompare(b.name));
  const goalies = team.players.filter((p) => p.position === 'G' && toiSeconds(p.toi) > 0);
  const tap = (key) => setSort(sort.key === key ? { key, desc: !sort.desc } : { key, desc: true });
  return html`
    <div class="bar">
      <div class="seg" role="group" aria-label="Team">
        ${['away', 'home'].map((s) => html`<button type="button" aria-pressed=${side === s} onClick=${() => setSide(s)}>${game[s].abbrev}</button>`)}
      </div>
      <span class="meta"><span class="you">■</span> You <span class="opp">■</span> Opp</span>
    </div>
    <div class="table-scroll"><table class="list skaters">
      <thead><tr><th scope="col">Skater</th>${SKATER_COLS.map(([key, label]) => html`<th scope="col" class="n"
        aria-sort=${sort.key === key ? (sort.desc ? 'descending' : 'ascending') : undefined}>
        <button type="button" class=${`sort ${sort.key === key ? 'on' : ''}`} onClick=${() => tap(key)}>${label}${sort.key === key ? (sort.desc ? '▾' : '▴') : ''}</button></th>`)}</tr></thead>
      <tbody>${skaters.map((p) => html`<tr class=${p.owner ? `tint-${p.owner}` : ''}>
        <th scope="row">${p.name}</th>
        ${SKATER_COLS.map(([key]) => html`<td class=${`n ${key === 'fantasy' ? 'strong' : ''}`}>${skaterCell(p, key)}</td>`)}
      </tr>`)}</tbody>
    </table></div>
    <table class="list fixed box">
      <thead><tr><th scope="col">Goalie</th><th scope="col" class="n">SA</th><th scope="col" class="n">SV</th><th scope="col" class="n">GA</th><th scope="col" class="n">SV%</th><th scope="col" class="n">FP</th></tr></thead>
      <tbody>${goalies.length ? goalies.map((p) => html`<tr class=${p.owner ? `tint-${p.owner}` : ''}>
        <th scope="row">${p.name}${p.decision ? html` <span class="meta">${p.decision}</span>` : ''}</th>
        <td class="n">${p.shots_against}</td><td class="n">${p.saves}</td><td class="n">${p.goals_against}</td>
        <td class="n">${p.shots_against ? (p.saves / p.shots_against).toFixed(3).replace(/^0/, '') : ''}</td>
        <td class="n strong">${f2(p.fantasy)}</td></tr>`)
        : html`<tr><td colspan="6" class="dim">Not in yet.</td></tr>`}</tbody>
    </table>
    <p class="foot">FP under your league's scoring. Tap a column header to sort.</p>`;
}

const TYPES = [
  ['goal', 'Goals', 'GOAL'], ['shot', 'Shots', 'SHOT'], ['penalty', 'Penalties', 'PEN'], ['hit', 'Hits', 'HIT'],
  ['blocked-shot', 'Blocked shots', 'BLK'], ['faceoff', 'Faceoffs', 'FO'], ['giveaway', 'Giveaways', 'GV'], ['takeaway', 'Takeaways', 'TK'],
];
const TYPE_OF = { 'shot-on-goal': 'shot', 'missed-shot': 'shot' };
const CODE = Object.fromEntries(TYPES.map(([id, , code]) => [id, code]));
// A shot's own chip: on net (SOG, which earns its points) or missed (MISS, which earns none).
const CHIP = { 'shot-on-goal': 'SOG', 'missed-shot': 'MISS' };

function Plays({ game }) {
  const [scope, setScope] = useState('all');
  const [menu, setMenu] = useState(false);
  useEscape(menu, () => setMenu(false));
  const [off, setOff] = useState(() => {
    try { return new Set(JSON.parse(store.get('fh.plays.off')) || []); } catch (_) { return new Set(); }
  });
  // Each change builds on the latest set (taps faster than a render must not drop one).
  const save = (change) => setOff((prev) => {
    const next = change(prev);
    store.set('fh.plays.off', JSON.stringify([...next]));
    return next;
  });
  const kind = (p) => TYPE_OF[p.type] || p.type;
  const shown = game.plays.filter((p) => CODE[kind(p)] && !off.has(kind(p))
    && (scope === 'all' || p.owners.includes(scope)));
  const periods = [];
  for (const p of shown) {
    const name = periodName(p.period, p.period_type);
    if (!periods.length || periods[periods.length - 1].name !== name) periods.push({ name, plays: [] });
    periods[periods.length - 1].plays.push(p);
  }
  return html`
    <div class="bar plays-bar">
      <div class="seg" role="group" aria-label="Whose plays">
        ${[['all', 'All'], ['me', 'Mine'], ['opp', 'Opponent']].map(([id, label]) => html`
          <button type="button" aria-pressed=${scope === id} onClick=${() => setScope(id)}>${label}</button>`)}
      </div>
      <button type="button" class="btn small" aria-expanded=${menu} aria-haspopup="true" onClick=${() => setMenu(!menu)}>
        Filter${off.size ? html` <span class="badge">${TYPES.length - off.size}</span>` : ''}</button>
      ${menu && html`<div class="type-menu" role="dialog" aria-label="Event types">
        <div class="type-head"><span>Event types</span>
          <button type="button" class="link" onClick=${() => save((prev) => (prev.size ? new Set() : new Set(TYPES.map(([id]) => id))))}>${off.size ? 'Select all' : 'Clear all'}</button></div>
        ${TYPES.map(([id, label, code]) => html`<label class="type-row">
          <input type="checkbox" checked=${!off.has(id)} onChange=${() => save((prev) => { const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n; })} />
          <span>${label}</span><span class=${`chip t-${code}`}>${code}</span></label>`)}
        <button type="button" class="btn" onClick=${() => setMenu(false)}>Done</button>
      </div>`}
    </div>
    ${!shown.length ? html`<p class="foot">No plays of this kind yet.</p>` : html`
    <table class="list fixed plays">
      <thead><tr><th scope="col" style="width: 56px">Time</th><th scope="col" style="width: 40px">Tm</th>
        <th scope="col" style="width: 54px">Event</th><th scope="col">Detail</th><th scope="col" class="n" style="width: 62px">FP</th></tr></thead>
      <tbody>${periods.map((per) => html`
        <tr class="per"><td colspan="5">${per.name} period</td></tr>
        ${per.plays.map((p) => { const fp = p.fp || {}; return html`<tr class=${p.owners.length ? `tint-${p.owners.includes('me') ? 'me' : 'opp'}` : ''}>
          <td class="n" style="text-align: left">${p.time}</td><td><b>${p.team}</b></td>
          <td><span class=${`chip t-${CHIP[p.type] || CODE[kind(p)]}`}>${CHIP[p.type] || CODE[kind(p)]}</span></td>
          <td class="wrap">${p.text}</td>
          <td class="n">${fp.me ? html`<span class="you">${signed(fp.me)}</span>` : ''}${fp.me && fp.opp ? ' ' : ''}${fp.opp ? html`<span class="opp">${signed(fp.opp)}</span>` : ''}</td>
        </tr>`; })}`)}</tbody>
    </table>`}`;
}

const mmss = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;

function Lines({ gameId, league, live }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [team, setTeam] = useState(1);
  const [win, setWin] = useState('recent');
  usePoll(async () => {
    try { const got = await api.lines(league, gameId); setData(got); setError(null); return got; } catch (e) { setError(e.message); throw e; }
  }, [league, gameId], () => live);
  if (error) return html`<div class="error" role="alert">${error}</div>`;
  if (!data) return html`<div class="empty">Loading…</div>`;
  if (!data.teams || !data.teams.length) return html`<div class="empty">No lines yet: the time-on-ice reports start with the game.</div>`;
  const t = data.teams[Math.min(team, data.teams.length - 1)];
  const w = t.windows[win];
  const side = (players) => (players.some((p) => p.owner === 'me') ? 'me' : players.some((p) => p.owner === 'opp') ? 'opp' : '');
  const unit = (label, u, sep) => html`<tr class=${side(u.players) ? `tint-${side(u.players)}` : ''}>
    <td class="unit">${label}${u.rank}</td>
    <td class=${`wrap ${u.thin ? 'dim' : ''}`}>${u.players.map((p) => p.name).join(sep)}</td>
    <td class="n">${mmss(u.shared)}</td></tr>`;
  return html`
    <div class="bar">
      <div class="seg" role="group" aria-label="Team">
        ${data.teams.map((x, i) => html`<button type="button" aria-pressed=${i === team} onClick=${() => setTeam(i)}>${x.team}</button>`)}
      </div>
      <div class="seg" role="group" aria-label="Window">
        <button type="button" aria-pressed=${win === 'recent'} onClick=${() => setWin('recent')}>Last 10m</button>
        <button type="button" aria-pressed=${win === 'game'} onClick=${() => setWin('game')}>Game</button>
      </div>
    </div>
    <table class="list fixed lines">
      <thead><tr><th scope="col" style="width: 44px">Unit</th><th scope="col">Players</th><th scope="col" class="n" style="width: 66px">Shared</th></tr></thead>
      <tbody>
        <tr class="per"><td colspan="3">Forwards · 5v5</td></tr>
        ${w.forwards.length ? w.forwards.map((u) => unit('L', u, ' – ')) : html`<tr><td colspan="3" class="dim">No 5v5 yet.</td></tr>`}
        <tr class="per"><td colspan="3">Defence · 5v5</td></tr>
        ${w.defence.length ? w.defence.map((u) => unit('D', u, ' – ')) : html`<tr><td colspan="3" class="dim">No 5v5 yet.</td></tr>`}
        <tr class="per"><td colspan="3">Special teams</td></tr>
        ${t.pp.length ? t.pp.map((u) => unit('PP', u, ', ')) : html`<tr><td colspan="3" class="dim">Under a minute of power play (${t.pp_seconds} s).</td></tr>`}
        ${t.pk.length ? t.pk.map((u) => unit('PK', u, ', ')) : html`<tr><td colspan="3" class="dim">Under a minute shorthanded (${t.sh_seconds} s).</td></tr>`}
      </tbody>
    </table>
    <p class="foot">${win === 'recent' ? `The last ${Math.round(w.seconds / 60)} minutes of 5v5` : `The game's ${Math.round(w.seconds / 60)} minutes of 5v5`}, from the NHL's time-on-ice reports.
      Grey units are thin (little shared ice). Rows tinted <span class="you">blue</span> hold your players, <span class="opp">orange</span> your opponent's.
      ${data.as_of && html`<br /><span class=${data.missing_seconds ? 'warn' : ''}>Reports through ${data.as_of}${data.missing_seconds ? ` (${data.missing_seconds} s missing)` : ''}</span>`}</p>`;
}

const VIEWS = [['summary', 'Summary'], ['box', 'Box'], ['plays', 'Plays'], ['lines', 'Lines']];

export function Game({ league, gameId, view, embedded }) {
  const [game, setGame] = useState(null);
  const [error, setError] = useState(null);
  usePoll(async () => {
    try { const got = await api.game(league, gameId); setGame(got); setError(null); return got; } catch (e) { setError(e.message); throw e; }
  }, [league, gameId], (g) => !!g && !DONE.has(g.state));
  const live = !!game && LIVE.has(game.state);
  const state = game && (DONE.has(game.state) ? (game.period_type && game.period_type !== 'REG' ? `Final/${game.period_type}` : 'Final')
    : LIVE.has(game.state) ? (game.intermission ? `INT · end ${periodName(game.period, game.period_type)}` : `${periodName(game.period, game.period_type)} ${game.clock || ''}`)
      : 'Not started');
  const lead = game && game.away.score !== game.home.score ? (game.away.score > game.home.score ? 'away' : 'home') : null;
  return html`
    ${!embedded && html`<header class="game-head">
      <a href="#/games" class="back" aria-label="Back to games"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m15 18-6-6 6-6"></path></svg></a>
      <span class="dim">${todayLabel()}${game ? ` · ${game.away.abbrev} @ ${game.home.abbrev}` : ''}</span>
      <span></span>
    </header>`}
    ${error && html`<div class="error" role="alert">${error}</div>`}
    ${game && html`<section class="scoreboard" aria-label="Score">
      <div class="side"><${Logo} abbr=${game.away.abbrev} big /><span class="nm"><span class="ab"><span class=${lead === 'home' ? 'dim2' : ''}>${game.away.abbrev}</span><${SituationTag} s=${game.situation} team=${game.away.abbrev} /></span><span class="meta">Away</span></span></div>
      <div class="mid"><span class="num mono"><span class=${lead === 'home' ? 'dim' : ''}>${game.away.score ?? 0}</span><span class="dash">–</span><span class=${lead === 'away' ? 'dim' : ''}>${game.home.score ?? 0}</span></span>
        <span class=${`st ${live ? 'live' : ''}`}>${live && !game.intermission && html`<span class="dot"></span>`}${state}</span>
        <${SituationLine} s=${game.situation} /></div>
      <div class="side right"><span class="nm"><span class="ab"><${SituationTag} s=${game.situation} team=${game.home.abbrev} /><span class=${lead === 'away' ? 'dim2' : ''}>${game.home.abbrev}</span></span><span class="meta">Home</span></span><${Logo} abbr=${game.home.abbrev} big /></div>
    </section>`}
    <nav class="game-views" aria-label="Game views">
      ${VIEWS.map(([id, label]) => html`<a href=${`#/game/${gameId}/${id}`} aria-current=${id === view ? 'page' : undefined}>${label}</a>`)}
    </nav>
    <main class="screen">
      ${!game ? (!error && html`<div class="empty">Loading…</div>`)
        : view === 'box' ? html`<${Box} game=${game} league=${league} />`
          : view === 'plays' ? html`<${Plays} game=${game} />`
            : view === 'lines' ? html`<${Lines} gameId=${gameId} league=${league} live=${live} />`
              : html`<${Summary} game=${game} />`}
    </main>`;
}
