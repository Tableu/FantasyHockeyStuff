// Games (plan P4; redesign step 4): a day's games from GET /games (today, or a day of the fantasy
// week from the day picker, GET /games/week), today's with the goal feed (GET /goals) in a sheet at
// the bottom; a game opens full screen (#/game/<id>/<view>) with its Summary, Box, Plays (GET
// /games/<id>) and Lines (GET /games/<id>/lines). Live, they poll every 5 s -- the server's feed
// cache (about 20 s) is what bounds the calls to the NHL. On a PC (`wide`) the list, the chosen
// game and the goal feed sit side by side: the game opens in the middle, the feed is a rail.
// Live, as NHL.com (the design's Games artboards, 2026-11-08): each clock ticks with a sweep under it,
// a whistle when play stops, the strength (5-on-4) by the clock, and a goal lights its card and the
// game's header up, pops the score and drops into the goal feed.
import { html, useState, useEffect, useRef } from './vendor/htm-preact-3.1.1.mjs';
import { api } from './api.js';
import { useEscape, Photo, DayPicker, dateOf } from './layout.js';

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

// ---------- the live clock (the design's Games animations, 2026-11-08) ----------
// Between the 5 s reads the clocks tick each second, as on NHL.com: a clock counts down from when
// its value was first seen (the server's feed copy can repeat it for ~20 s), while it runs -- the
// game clock and the power play stop for a whistle, an intermission's clock always runs.
const firstSeen = new Map();          // "<game>|<what>|<value>" -> when that value first arrived
const secsOf = (t) => (t ? t.split(':').reduce((m, x) => m * 60 + Number(x), 0) : 0);
const pad = (n) => String(n).padStart(2, '0');
const mmss2 = (x) => `${pad(Math.floor(x / 60))}:${pad(x % 60)}`;           // 02:10, as NHL.com
function ticking(id, what, text, runs, now) {
  if (!text || !runs) return secsOf(text);
  const key = `${id}|${what}|${text}`;
  if (!firstSeen.has(key)) {
    if (firstSeen.size > 400) firstSeen.clear();
    firstSeen.set(key, now);
  }
  return Math.max(0, secsOf(text) - Math.floor((now - firstSeen.get(key)) / 1000));
}

// A second's re-render while `on` (a game live): the clocks tick, a goal's moment ends.
function useNow(on) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!on) return undefined;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [on]);
  return now;
}

// A live game's man advantage (the server's situation) beside the team that has it: "PP 1:42"
// (the power play's time left, running with the clock) or "Pulled" for a pulled goalie.
function SituationTag({ g, team, now }) {
  const s = g.situation;
  if (!s || s.team !== team) return null;
  if (s.kind === 'pulled') return html`<span class="sit pulled">Pulled</span>`;
  const left = s.left ? ticking(g.id, 'pp', s.left, g.running && !g.intermission, now) : null;
  return html`<span class="sit pp">PP${left !== null ? ` ${Math.floor(left / 60)}:${pad(left % 60)}` : ''}</span>`;
}

// ...and the skaters on the ice, as NHL.com: "5-on-4", "5-on-3", "6-on-5" with a pulled goalie.
const strengthOf = (s) => (s ? `${s.skaters[0]}-on-${s.skaters[1]}` : '');

const Whistle = () => html`<span class="whistle" role="img" aria-label="Play stopped" title="Play stopped"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="9" cy="14" r="5"></circle><path d="M13 11h9v4h-6"></path><path d="M9 14h.01"></path><path d="M6 5l1.2 2"></path><path d="M10.5 3.5v2.5"></path><path d="M15 5l-1.2 2"></path></svg></span>`;

// A live game's period and clock, as NHL.com's scores: the period in a grey pill ("2nd", "1st INT"),
// the time (MM:SS) with the sweep under it -- white while the clock runs, grey through an
// intermission, a still line and a whistle when play stops. `center` (the game's header): the
// pill and time stay dead centre, the whistle hangs off the right. Not live: stateLabel's text.
function GameClock({ g, now, center }) {
  if (!LIVE.has(g.state)) return html`<span class="gclock-text">${stateLabel(g)}</span>`;
  const per = periodName(g.period, g.period_type);
  const int = g.intermission, runs = !int && g.running;
  const time = mmss2(ticking(g.id, 'clock', g.clock, int || runs, now));
  return html`<span class=${`gclock ${center ? 'center' : ''}`}>
    <span class="pill">${int ? `${per} INT` : per}</span>
    <span class="clk"><span class="t">${time}</span><span class=${`track ${int ? 'calm' : runs ? '' : 'still'}`} aria-hidden="true">${(int || runs) && html`<span></span>`}</span></span>
    ${!int && !runs && html`<${Whistle} />`}
  </span>`;
}

// The goals since the last read: a live game whose score went up lights up for 3 s -- {id: {side, at}}.
const GOAL_MS = 3000;
function useGoalFlash() {
  const scores = useRef(new Map());
  const [flash, setFlash] = useState({});
  const see = (games) => {
    const fresh = {};
    for (const g of games) {
      const was = scores.current.get(g.id), a = g.away.score ?? 0, h = g.home.score ?? 0;
      if (was && LIVE.has(g.state)) {
        if (a > was.a) fresh[g.id] = { side: 'away', at: Date.now() };
        else if (h > was.h) fresh[g.id] = { side: 'home', at: Date.now() };
      }
      scores.current.set(g.id, { a, h });
    }
    if (Object.keys(fresh).length) setFlash((f) => ({ ...f, ...fresh }));
  };
  const active = (id, now) => (flash[id] && now - flash[id].at < GOAL_MS ? flash[id] : null);
  return [active, see];
}

// A score, popping when its team just scored.
const Score = ({ value, pop }) => (pop ? html`<span class="pop">${value}</span>` : value);

// "2nd 14:21", "INT · end 1st", "Final", "Final/OT", or the start time.
export function stateLabel(g) {
  if (DONE.has(g.state)) return g.period_type && g.period_type !== 'REG' ? `Final/${g.period_type}` : 'Final';
  if (LIVE.has(g.state)) {
    if (g.intermission) return `INT · end ${periodName(g.period, g.period_type)}`;
    return `${periodName(g.period, g.period_type)} ${g.clock || ''}`.trim();
  }
  return clockTime(g.start_utc);
}

// "Sat Oct 10": a game's day (its date, else today).
function dayLabel(iso) {
  const t = iso ? dateOf(iso) : new Date();
  return `${t.toLocaleDateString('en-US', { weekday: 'short' })} ${t.toLocaleDateString('en-US', { month: 'short' })} ${t.getDate()}`;
}

// The day the list shows, kept while the app is open: back from a game, the list is on its day.
let shownDay = 'today';

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

// A goal's key in the feed, and when this screen first saw it (the first read seeds them all:
// only goals arriving after it drop in).
const goalKey = (g) => `${g.game_id}|${g.period}|${g.time}|${g.team}`;

function GoalFeed({ goals, open: opened, onToggle, rail, now }) {
  const [filter, setFilter] = useState('all');
  const seen = useRef(null);
  if (goals) {
    if (seen.current === null) seen.current = new Map(goals.goals.map((g) => [goalKey(g), 0]));
    for (const g of goals.goals) if (!seen.current.has(goalKey(g))) seen.current.set(goalKey(g), Date.now());
  }
  const isNew = (g) => !!seen.current && now - (seen.current.get(goalKey(g)) || 0) < GOAL_MS;
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
            return html`<tr class=${`${side ? `tint-${side}` : ''} ${isNew(g) ? 'enter' : ''}`}>
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

// A game's card: the period pill and clock (or the start time, or Final), the strength or GOAL at
// the right; each team's logo, PP tag, shots and score. A goal lights the card up for 3 s.
function card(g, href, current, now, goal) {
  const done = DONE.has(g.state), started = done || LIVE.has(g.state);
  const lead = started && g.away.score !== g.home.score ? (g.away.score > g.home.score ? 'away' : 'home') : null;
  const strength = LIVE.has(g.state) && !g.intermission ? strengthOf(g.situation) : '';
  return html`<a class="game-card" href=${href} aria-current=${current ? 'page' : undefined}
    aria-label=${`${g.away.abbrev} at ${g.home.abbrev}, ${stateLabel(g)}`}>
    ${goal && html`<span class="goal-flash" aria-hidden="true"></span>`}
    <div class="game-top">
      <${GameClock} g=${g} now=${now} />
      ${goal ? html`<span class="goal-badge">GOAL</span>` : strength && html`<span class="strength">${strength}</span>`}
    </div>
    ${['away', 'home'].map((side) => html`<div class="game-team">
      <${Logo} abbr=${g[side].abbrev} />
      <span class="tn"><span class="ab"><span class=${done && lead && lead !== side ? 'dim2' : ''}>${g[side].abbrev}</span><${SituationTag} g=${g} team=${g[side].abbrev} now=${now} /></span>
        ${started && g[side].sog !== null && html`<span class="sog">SOG: ${g[side].sog}</span>`}</span>
      <span class=${`score ${done && lead && lead !== side ? 'dim2' : ''}`}>${started ? html`<${Score} value=${g[side].score} pop=${goal && goal.side === side} />` : ''}</span>
    </div>`)}
  </a>`;
}

export function Games({ league, version, wide, selected, view }) {
  const [games, setGames] = useState(null);
  const [goals, setGoals] = useState(null);
  const [week, setWeek] = useState(null);
  const [error, setError] = useState(null);
  const [open, setOpen] = useState(false);
  const [day, setDay] = useState(shownDay);
  const [goalOf, seeGoals] = useGoalFlash();
  const today = day === 'today';
  const pick = (d) => { shownDay = d; setDay(d); setGames(null); setOpen(false); };

  useEffect(() => {
    api.gamesWeek(league).then((got) => setWeek(got.week)).catch(() => setWeek(null));
  }, [league, version]);

  usePoll(async () => {
    try {
      // The goal feed is today's alone.
      const [g, gl] = await Promise.all([api.games(league, today ? undefined : day), today ? api.goals(league) : null]);
      setGames(g); setGoals(gl); setError(null); seeGoals(g.games);
      return g;
    } catch (e) {
      setError(e.message);
      throw e;
    }
  }, [league, version, day], (g) => !!g && g.games.some((x) => LIVE.has(x.state)));

  const live = games && games.games.some((x) => LIVE.has(x.state));
  const now = useNow(!!live);
  const count = games ? `${games.games.length} game${games.games.length === 1 ? '' : 's'}` : '';
  const picker = week && html`<${DayPicker} week=${week} day=${day} onDay=${pick}
    right=${(d) => [d.games === null ? '' : `${d.games} game${d.games === 1 ? '' : 's'}`, 'dim']} />`;
  const none = today ? 'No NHL games today.' : 'No NHL games that day.';
  if (wide) {
    // A game card keeps the view the open game shows, so moving between games stays on Box (say).
    return html`<div class="games-wide">
      <section class="games-list" aria-label="Today's games">
        <header class="screen-head">
          ${picker || html`<span class="h">Games <span class="dim">${dayLabel()}</span></span>`}
          ${live ? html`<span class="live-tag"><span class="dot"></span>Live · 5s</span>` : html`<span class="dim games-count">${count}</span>`}
        </header>
        ${error && html`<div class="error" role="alert">${error}</div>`}
        ${!games ? html`<div class="empty">Loading…</div>` : !games.games.length ? html`<div class="empty">${none}</div>`
          : html`<div class="game-grid">${games.games.map((g) => card(g, `#/game/${g.id}/${view || 'summary'}`, g.id === selected, now, goalOf(g.id, now)))}</div>`}
      </section>
      <section class="game-pane" aria-label="Game">
        ${selected ? html`<${Game} key=${selected} league=${league} gameId=${selected} view=${view} embedded />`
          : html`<div class="empty">Choose a game for its summary, box score, plays and lines.</div>`}
      </section>
      ${today && html`<${GoalFeed} goals=${goals} rail now=${now} />`}
    </div>`;
  }
  // The phone: the app's header (the team selector) is above it; the day picker and the day's count.
  return html`
    <div class="games-bar">${picker}<span class="dim">${count}</span></div>
    <main class=${`screen games-screen ${today ? '' : 'no-feed'}`}>
      ${error && html`<div class="error" role="alert">${error}</div>`}
      ${!games ? html`<div class="empty">Loading…</div>` : !games.games.length ? html`<div class="empty">${none}</div>` : html`
      <div class="game-grid">
        ${games.games.map((g) => card(g, `#/game/${g.id}/summary`, false, now, goalOf(g.id, now)))}
      </div>
      <p class="foot">Tap a game for its summary.</p>`}
    </main>
    ${today && html`<${GoalFeed} goals=${goals} open=${open} onToggle=${() => setOpen(!open)} now=${now} />`}`;
}

// ---------- one game ----------

// Game stats, as NHL.com's gamecenter: both numbers at the ends, the stat between, one bar split by
// each team's share (the leader light, the other dark; for penalty minutes and giveaways the
// lower leads). [category, label, shown as a %, the count shown under it, lower is better]
const STAT_ROWS = [
  ['sog', 'Shots on goal'], ['faceoffWinningPctg', 'Faceoff %', true, 'faceoffWins'],
  ['powerPlayPctg', 'Power play %', true, 'powerPlay'], ['pim', 'Penalty minutes', false, null, true],
  ['hits', 'Hits'], ['blockedShots', 'Blocked shots'], ['giveaways', 'Giveaways', false, null, true], ['takeaways', 'Takeaways'],
];

function StatBar({ label, a, h, aText, hText, aSub, hSub, lower, awayName, homeName }) {
  const known = a !== null && h !== null;
  const aLead = known && (lower ? a < h : a > h), hLead = known && (lower ? h < a : h > a);
  const total = known ? a + h : 0;
  const aShare = total ? a / total : 0.5, hShare = total ? h / total : 0.5;
  return html`<div class=${`stat ${known ? '' : 'none'}`}>
    <div class="stat-top"><span class=${`v ${aLead ? 'lead' : ''}`}>${aText}</span><span class="l">${label}</span><span class=${`v r ${hLead ? 'lead' : ''}`}>${hText}</span></div>
    ${aSub && html`<div class="stat-sub"><span>${aSub}</span><span>${hSub}</span></div>`}
    <div class="stat-bar" role="img" aria-label=${known ? `${label}: ${awayName} ${aText}, ${homeName} ${hText}` : `${label}: not played yet`}>
      <span class=${`l ${aLead ? 'hi' : ''}`} style=${`flex: ${aShare} 1 0%`}></span><span class=${`r ${hLead ? 'hi' : ''}`} style=${`flex: ${hShare} 1 0%`}></span>
    </div>
  </div>`;
}

// The periods a game has had or will have: 1st to 3rd, then any overtime it went to.
function periodsOf(game, seen) {
  const last = Math.max(3, game.period || 0, ...seen);
  return Array.from({ length: last }, (_, i) => i + 1);
}
const periodTitle = (n) => (n <= 3 ? `${ORD[n]} period` : n === 4 ? 'Overtime' : `Overtime ${n - 3}`);

// Goals by period, a card each as the goal feed's rows: the scorer's portrait, name (his goal
// count), assists, team · strength · time; the score after it (the scoring team's first) and the
// fantasy points it earned your players or your opponent's, tinted as the feed.
function Goals({ game }) {
  const goals = game.goals || [];
  const started = LIVE.has(game.state) || DONE.has(game.state);
  if (!started) return null;
  const played = DONE.has(game.state) ? 99 : game.period || 0;
  return html`<h2 class="sum-h">Goals</h2>
    ${periodsOf(game, goals.map((g) => g.period)).map((n) => {
      const here = goals.filter((g) => g.period === n);
      if (!here.length && n > 3 && n > played) return null;
      return html`<h3 class="per">${periodTitle(n)}</h3>
        ${!here.length ? html`<p class="per-none">${n < played ? 'No goals' : n === played ? 'No goals yet' : 'Not played yet'}</p>`
          : here.map((g) => {
            const by = { me: 0, opp: 0 };
            for (const q of [g.scorer, ...g.assists]) if (q.owner) by[q.owner] += q.points;
            const side = by.me ? 'me' : by.opp ? 'opp' : '';
            const away = g.team === game.away.abbrev;
            const score = `${away ? g.away_score : g.home_score}–${away ? g.home_score : g.away_score} ${g.team}`;
            return html`<div class=${`goal-card ${side ? `tint-${side}` : ''}`}>
              <${Photo} src=${g.scorer.photo} name=${g.scorer.name} size=${34} />
              <span class="who"><span class="nm">${g.scorer.name} <span class="meta">(${g.scorer.to_date})</span></span>
                <span class="meta">${g.assists.length ? g.assists.map((x) => x.name).join(', ') : 'Unassisted'}</span>
                <span class="meta dim2">${g.team} · ${g.strength} · ${g.time}</span></span>
              <span class="sc"><span class="s">${score}</span>${by.me ? html`<span class="you">${signed(by.me)}</span>` : ''}${by.opp ? html`<span class="opp">${signed(by.opp)}</span>` : ''}</span>
              ${g.clip ? html`<a class="clip" href=${g.clip} target="_blank" rel="noopener" aria-label=${`Highlight: ${g.scorer.name}`}>
                <svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M7 4v16l13-8z"></path></svg></a>` : html`<span></span>`}
            </div>`;
          })}`;
    })}`;
}

function Summary({ game }) {
  const stats = Object.fromEntries((game.team_stats || []).map((s) => [s.category, s]));
  const names = { awayName: game.away.abbrev, homeName: game.home.abbrev };
  const pct = (x) => `${Math.round(x * 100)}%`;
  const rows = STAT_ROWS.filter(([key]) => stats[key]).map(([key, label, isPct, subKey, lower]) => {
    const s = stats[key], sub = subKey && stats[subKey];
    const a = Number(s.awayValue), h = Number(s.homeValue);
    return { label, a, h, lower, aText: isPct ? pct(a) : String(s.awayValue), hText: isPct ? pct(h) : String(s.homeValue),
      aSub: sub ? String(sub.awayValue) : '', hSub: sub ? String(sub.homeValue) : '' };
  });
  const shots = Object.fromEntries((game.shots_by_period || []).map((p) => [p.periodDescriptor.number, p]));
  const shotPeriods = periodsOf(game, Object.keys(shots).map(Number)).filter((n) => n <= 3 || shots[n]);
  return html`
    <${Goals} game=${game} />
    ${rows.length > 0 && html`<h2 class="sum-h">Game stats</h2>
    <div class="stats">${rows.map((r) => html`<${StatBar} ...${r} ...${names} />`)}</div>`}
    ${(game.shots_by_period || []).length > 0 && html`<h2 class="sum-h">Shots on goal</h2>
    <div class="stats">${shotPeriods.map((n) => {
      const p = shots[n];
      return p ? html`<${StatBar} label=${periodTitle(n)} a=${p.away} h=${p.home} aText=${String(p.away)} hText=${String(p.home)} ...${names} />`
        : html`<${StatBar} label=${periodTitle(n)} a=${null} h=${null} aText="–" hText="–" ...${names} />`;
    })}</div>`}
    <p class="foot">Each bar splits the stat between the teams, ${game.away.abbrev} on the left, ${game.home.abbrev} on the right; the leader is bright (for penalty minutes and giveaways, the lower). Faceoffs: wins / faceoffs. Power play: goals / chances.${LIVE.has(game.state) ? ' Updates every 5 s while the game is live.' : ''}</p>`;
}

// FP first, so it stays on screen while the rest scroll sideways on a phone. PP / SH TOI come from
// the shift reports (as Lines): blank until they are posted.
const SKATER_COLS = [['fantasy', 'FP'], ['goals', 'G'], ['assists', 'A'], ['shots', 'SOG'], ['hits', 'HIT'],
  ['blocks', 'BLK'], ['pim', 'PIM'], ['faceoff_wins', 'FOW'], ['faceoff_pct', 'FO%'], ['toi', 'TOI'],
  ['pp_toi', 'PP TOI'], ['sh_toi', 'SH TOI']];
const TOI_KEYS = new Set(['toi', 'pp_toi', 'sh_toi']);
const POSITION = { L: 'LW', R: 'RW' };          // the box score's letters, as the league writes them
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
        <th scope="row">${p.name}<span class="pos">${POSITION[p.position] || p.position || ''}</span></th>
        ${SKATER_COLS.map(([key]) => html`<td class=${`n ${key === 'fantasy' ? 'strong' : ''}`}>${skaterCell(p, key)}</td>`)}
      </tr>`)}</tbody>
    </table></div>
    <table class="list fixed box">
      <thead><tr><th scope="col">Goalie</th><th scope="col" class="n">SA</th><th scope="col" class="n">SV</th><th scope="col" class="n">GA</th><th scope="col" class="n">SV%</th><th scope="col" class="n">FP</th></tr></thead>
      <tbody>${goalies.length ? goalies.map((p) => html`<tr class=${p.owner ? `tint-${p.owner}` : ''}>
        <th scope="row">${p.name}<span class="pos">G</span>${p.decision ? html` <span class="meta">${p.decision}</span>` : ''}</th>
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
  const [goalOf, seeGoals] = useGoalFlash();
  usePoll(async () => {
    try { const got = await api.game(league, gameId); setGame(got); setError(null); seeGoals([got]); return got; } catch (e) { setError(e.message); throw e; }
  }, [league, gameId], (g) => !!g && !DONE.has(g.state));
  const live = !!game && LIVE.has(game.state);
  const now = useNow(live);
  const goal = game && goalOf(game.id, now);
  const strength = live && !game.intermission ? strengthOf(game.situation) : '';
  const lead = game && game.away.score !== game.home.score ? (game.away.score > game.home.score ? 'away' : 'home') : null;
  return html`
    ${!embedded && html`<header class="game-head">
      <a href="#/games" class="back" aria-label="Back to games"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m15 18-6-6 6-6"></path></svg></a>
      <span class="dim">${dayLabel(game && game.date)}${game ? ` · ${game.away.abbrev} @ ${game.home.abbrev}` : ''}</span>
      <span></span>
    </header>`}
    ${error && html`<div class="error" role="alert">${error}</div>`}
    ${game && html`<section class="scoreboard" aria-label="Score">
      ${goal && html`<span class="goal-flash" aria-hidden="true"></span>`}
      <div class="side"><${Logo} abbr=${game.away.abbrev} big /><span class="nm"><span class="ab"><span class=${lead === 'home' ? 'dim2' : ''}>${game.away.abbrev}</span><${SituationTag} g=${game} team=${game.away.abbrev} now=${now} /></span><span class="meta">Away</span></span></div>
      <div class="mid"><span class="num mono"><span class=${lead === 'home' ? 'dim' : ''}><${Score} value=${game.away.score ?? 0} pop=${goal && goal.side === 'away'} /></span><span class="dash">–</span><span class=${lead === 'away' ? 'dim' : ''}><${Score} value=${game.home.score ?? 0} pop=${goal && goal.side === 'home'} /></span></span>
        <span class="st">${game.state === 'FUT' || game.state === 'PRE' ? 'Not started' : html`<${GameClock} g=${game} now=${now} center />`}</span>
        <span class="st-sub">${goal ? html`<span class="goal-badge">GOAL</span>` : strength && html`<span class="strength">${strength}</span>`}</span></div>
      <div class="side right"><span class="nm"><span class="ab"><${SituationTag} g=${game} team=${game.home.abbrev} now=${now} /><span class=${lead === 'away' ? 'dim2' : ''}>${game.home.abbrev}</span></span><span class="meta">Home</span></span><${Logo} abbr=${game.home.abbrev} big /></div>
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
