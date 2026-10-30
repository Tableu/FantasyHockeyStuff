// The Roster screen (plan P1; redesign plan app-redesign.md, step 1): your lineup, bench and IR
// from GET /roster for a day of the fantasy week, with live points from GET /tonight on today.
// The day picker holds the whole week (a day gone: its plan, finals and points; a day ahead: its
// games and projection); Season is its own button (tap a header to sort). The day on a phone is a card per player (the user, 2026-10-20): name, P%
// and game, xPts on the right, his scoring stats and TOI as tags below, his points under xPts. On
// a PC (`wide`) the same two views as a table, the box line in its own columns.
import { html, useState, useEffect } from './vendor/htm-preact-3.1.1.mjs';
import { api } from './api.js';
import { NoPlan, Photo, DayPicker, shortDay } from './layout.js';
import { stateLabel } from './games.js';

const TONIGHT_EVERY_MS = 20000;     // the server's NHL feed cache is about 20 s
const STARTED = new Set(['LIVE', 'CRIT', 'OFF', 'FINAL']);
const FLAG_CLASS = { DTD: 'warn', GTD: 'warn', Q: 'warn', O: 'bad', OUT: 'bad', IR: 'bad', SUSP: 'bad' };

const store = {
  get(key) { try { return localStorage.getItem(key); } catch (_) { return null; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch (_) { /* private window */ } },
};

const f2 = (x) => (x === null || x === undefined ? '' : Number(x).toFixed(2));


// The plan's puck times are UTC without a zone ("2026-10-10 23:00:00").
function puckTime(utc) {
  if (!utc) return '';
  const t = new Date(utc.replace(' ', 'T') + 'Z');
  const h = t.getHours() % 12 || 12;
  const m = String(t.getMinutes()).padStart(2, '0');
  return `${h}:${m} ${t.getHours() < 12 ? 'AM' : 'PM'}`;
}

// Under the name (the user, 2026-10-20): the opponent, @ on the road, and the score, his team's
// first, with the period and clock or Final -- before the puck drops, its time; no game data
// (tomorrow's plan before its games are read), the puck time alone.
export function gameLine(row, game) {
  if (!game) return row.puck_utc ? puckTime(row.puck_utc) : '';
  const vs = `${game.home ? '' : '@'}${game.vs}`;
  if (!STARTED.has(game.state)) return `${vs} ${puckTime(game.start_utc.replace('T', ' ').replace('Z', ''))}`;
  return `${vs} ${game.score ?? 0}–${game.vs_score ?? 0} · ${stateLabel(game)}`;
}

// P%: his chance to play, or once his game has started ✓ -- a goalie who sat (no box line, or
// 0:00) didn't play. A day ahead without its own plan has none (its xPts has the chance in it).
function playChance(row, live, goalie) {
  const game = live && live.game;
  const line = live && live.line;
  const started = (game && STARTED.has(game.state)) || row.locked;
  if (started && game) {
    return { prob: goalie && (!line || !line.toi || line.toi === '00:00') ? (line ? 'DNP' : '…') : '✓', probWarn: false };
  }
  if (row.p_plays === null || row.p_plays === undefined) return { prob: '—', probWarn: false };
  return { prob: String(Math.round(row.p_plays * 100)), probWarn: row.p_plays < 0.9 };
}

const probText = (prob) => (/^\d+$/.test(prob) ? `${prob}%` : prob === '—' ? '' : prob);

// His box line as tags: the league's scoring stats, the Season columns (the user, 2026-10-24), a
// goalie's SV% after them, then TOI; a stat he has none of tonight is left out.
function boxTags(line, goalie, columns) {
  if (!line) return [];
  const tags = columns.filter(([key]) => line[key]).map(([key, label]) => [label, line[key]]);
  if (goalie && line.shots_against) tags.push(['SV%', svPct(line)]);
  if (line.toi) tags.push(['TOI', line.toi]);
  return tags;
}

const svPct = (line) => (line.saves / line.shots_against).toFixed(3).replace(/^0/, '');

const Tags = ({ tags }) => html`<span class="tags">${tags.map(([k, v]) => html`<span class="tag"><span class="k">${k}</span> ${v}</span>`)}</span>`;

// The PC table's box columns: the league's scoring stats but GP, a goalie's SV%, TOI.
function boxColumns(columns, goalie) {
  return [...columns.filter(([key]) => key !== 'gp'), ...(goalie ? [['sv_pct', 'SV%']] : []), ['toi', 'TOI']];
}

function dayCells(row, live, goalie, columns) {
  const line = live && live.line;
  const { prob, probWarn } = playChance(row, live, goalie);
  return [
    { text: row.xpts === null || row.xpts === undefined ? '—' : f2(row.xpts), cls: 'f', weight: 600 },
    { text: line ? f2(line.fantasy) : '', cls: 'f you', weight: 600 },
    { text: probText(prob), cls: `f ${probWarn ? 'warn' : 'dim'}` },
    { text: gameLine(row, live && live.game), cls: 'txt game-col' },
    ...boxColumns(columns, goalie).map(([key], i) => {
      const v = !line ? '' : key === 'sv_pct' ? (line.shots_against ? svPct(line) : '') : line[key];
      return { text: v === undefined || v === null || v === 0 ? '' : String(v), cls: i ? '' : 'sep' };
    }),
  ];
}

function seasonCells(row, columns) {
  const s = row.season || {};
  const cells = [
    { text: s.gp ? f2(s.fp) : '', cls: 'f' },
    { text: s.fp_per_game === null ? '' : f2(s.fp_per_game), cls: 'f', weight: 600 },
    { text: row.ros === null || row.ros === undefined ? '' : Math.round(row.ros), cls: 'f' },
  ];
  for (const [key] of columns) cells.push({ text: s[key] === undefined || s[key] === null ? '' : String(s[key]) });
  if (row.goalie) cells.push({ text: s.sv_pct ? s.sv_pct.toFixed(3).replace(/^0/, '') : '' });
  return cells;
}

// What a header's abbreviation stands for, shown on hover.
const TITLES = {
  xPts: 'Expected fantasy points that day', Live: 'Fantasy points so far tonight', Pts: 'Fantasy points that day',
  'P%': 'Chance he plays', Game: 'The game: puck drop, the score once it starts, the final after',
  FP: 'Fantasy points this season', 'FP/g': 'Fantasy points per game this season',
  ROS: 'Projected fantasy points for the rest of the season',
};

function heads(view, goalie, columns, gone) {
  const F = (label, strong) => ({ label, cls: 'f', strong });
  if (view === 'day') {
    return [F('xPts', true), F(gone ? 'Pts' : 'Live'), F('P%'), { label: 'Game', cls: 'txt game-col' },
      ...boxColumns(columns, goalie).map(([, label], i) => ({ label, cls: i ? '' : 'sep' }))]
      .map((c) => ({ ...c, title: TITLES[c.label] }));
  }
  return [{ ...F('FP'), key: 'fp' }, { ...F('FP/g', true), key: 'fp_per_game' }, { ...F('ROS'), key: 'ros' },
    ...columns.map(([key, label]) => ({ label, key })), ...(goalie ? [{ label: 'SV%', key: 'sv_pct' }] : [])]
    .map((c) => ({ ...c, title: TITLES[c.label] }));
}

// A Season column's value for sorting: the season's stat, or ROS from the row.
const sortValue = (row, key) => (key === 'ros' ? row.ros : (row.season || {})[key]);

// Rows by a Season column, missing values last either way; ties keep the roster's order.
function sorted(rows, sort) {
  if (!sort) return rows;
  const dir = sort.desc ? -1 : 1;
  return rows.map((row, i) => [row, i]).sort(([a, i], [b, j]) => {
    const x = sortValue(a, sort.key), y = sortValue(b, sort.key);
    const xm = x === null || x === undefined, ym = y === null || y === undefined;
    if (xm || ym) return xm && ym ? i - j : xm ? 1 : -1;
    return x === y ? i - j : (x < y ? -1 : 1) * dir;
  }).map(([row]) => row);
}

const groupColumns = (group, columns) => (group.goalie ? columns.goalie : columns.skater);

// Name, eligible positions and flag: the same on a card and in the table.
function Who({ row }) {
  return html`<span class=${`pname ${FLAG_CLASS[row.flag] || ''}`} title=${row.player}>${row.name}</span><span class="elig" title="Eligible positions">${row.positions}</span>
    ${row.flag && html` <span class=${`flag ${FLAG_CLASS[row.flag] || 'warn'}`}>${row.flag}</span>`}`;
}

// One group's rows in the shared table; `span` is the widest group's column count, so a group with
// fewer columns (the goalies) is padded out to the table's edge.
function Group({ group, view, columns, span, live, gone, sort, onSort }) {
  const cols = heads(view, group.goalie, groupColumns(group, columns), gone);
  const pad = Array.from({ length: Math.max(0, span - cols.length) });
  const rows = view === 'day' ? group.rows : sorted(group.rows, sort);
  const head = (c) => {
    if (!c.key) return c.label;
    const on = sort && sort.key === c.key;
    return html`<button type="button" class=${`sort ${on ? 'on' : ''}`} onClick=${() => onSort(c.key)}
      aria-label=${`Sort by ${c.label}`}>${c.label}${on ? (sort.desc ? ' ▾' : ' ▴') : ''}</button>`;
  };
  return html`<tbody>
    <tr class="sub">
      <th scope="colgroup" class="stick">${group.name}</th>
      ${cols.map((c) => html`<th scope="col" class=${c.cls || ''} style=${c.strong ? 'color: var(--text)' : ''} title=${c.title}
        aria-sort=${c.key && sort && sort.key === c.key ? (sort.desc ? 'descending' : 'ascending') : undefined}>${head(c)}</th>`)}
      ${pad.map(() => html`<th aria-hidden="true"></th>`)}
    </tr>
    ${rows.map((row) => {
      const cells = view === 'day'
        ? dayCells(row, live && live[row.player_id], group.goalie, groupColumns(group, columns))
        : seasonCells(row, groupColumns(group, columns));
      return html`
        <tr>
          <th scope="row" class="stick">
            <span class="slot">${row.slot}</span>
            <${Photo} src=${row.photo} name=${row.name} size=${30} />
            <${Who} row=${row} />
          </th>
          ${cells.map((c) => html`<td class=${c.cls || ''} style=${c.weight ? `font-weight: ${c.weight}` : ''}>${c.text}</td>`)}
          ${pad.map(() => html`<td></td>`)}
        </tr>`;
    })}
  </tbody>`;
}

// The day on a phone: one card per player, the group's name sticking at the top.
function DayGroup({ group, columns, live }) {
  return html`<section class="tn" aria-label=${group.name}>
    <h3 class="tn-head">${group.name}<span class="cap">Expected points</span></h3>
    ${group.rows.map((row) => {
      const mine = live && live[row.player_id];
      const line = mine && mine.line;
      const { prob, probWarn } = playChance(row, mine, group.goalie);
      const tags = boxTags(line, group.goalie, groupColumns(group, columns));
      const game = gameLine(row, mine && mine.game) || (row.where === 'ir' ? 'On IR' : 'No game');
      return html`<div class="tn-row">
        <span class="slot">${row.slot}</span>
        <${Photo} src=${row.photo} name=${row.name} size=${34} />
        <span class="who">
          <span><${Who} row=${row} />
          <span class=${`p ${probWarn ? 'warn' : ''}`} title="Chance he plays">${probText(prob)}</span></span>
          <span class="game">${game}</span>
        </span>
        <span class="x" title="Expected fantasy points that day">${row.xpts === null || row.xpts === undefined ? '—' : f2(row.xpts)}</span>
        ${line && html`
          <${Tags} tags=${tags} />
          <span class="live" title="Fantasy points that day">${f2(line.fantasy)}</span>`}
      </div>`;
    })}
  </section>`;
}

export function Roster({ league, version, wide }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [tonight, setTonight] = useState(null);
  const [liveError, setLiveError] = useState(null);
  // The day's view or the Season's: kept on this device ('tonight' is the day's, as it was named).
  const [view, setView] = useState(() => (store.get('fh.roster.view') === 'season' ? 'season' : 'day'));
  // The day shown: 'today' or a date this week (not kept: a day goes stale).
  const [day, setDay] = useState('today');
  // The Season sort: {key, desc}, or null for the roster's order; kept on this phone.
  const [sort, setSort] = useState(() => {
    try { return JSON.parse(store.get('fh.roster.sort')) || null; } catch (_) { return null; }
  });
  // Season is today's roster, whatever day was shown.
  const asked = view === 'season' ? 'today' : day;

  useEffect(() => {
    let gone = false;
    setError(null);
    api.roster(league, asked)
      .then((got) => { if (!gone) setData(got); })
      .catch((e) => { if (!gone) { setData(null); setError(e); } });
    return () => { gone = true; };
  }, [league, version, asked]);

  useEffect(() => {
    let gone = false;
    const load = () => {
      api.tonight(league)
        .then((got) => { if (!gone) { setTonight(got); setLiveError(null); } })
        .catch((e) => { if (!gone) setLiveError(e.message); });
    };
    // Once now; then every tick while the app is on screen, and at once when it comes back.
    load();
    const timer = setInterval(() => { if (document.visibilityState === 'visible') load(); }, TONIGHT_EVERY_MS);
    const onShow = () => { if (document.visibilityState === 'visible') load(); };
    document.addEventListener('visibilitychange', onShow);
    return () => { gone = true; clearInterval(timer); document.removeEventListener('visibilitychange', onShow); };
  }, [league, version]);

  const choose = (v) => { setView(v); store.set('fh.roster.view', v === 'season' ? 'season' : 'tonight'); };
  const pickDay = (d) => { setDay(d); choose('day'); };
  // A column's first tap sorts it high to low, the second low to high, the third back to the roster.
  const sortBy = (key) => {
    const next = !sort || sort.key !== key ? { key, desc: true } : sort.desc ? { key, desc: false } : null;
    setSort(next);
    store.set('fh.roster.sort', JSON.stringify(next));
  };

  const todayLive = tonight && data && data.week && tonight.date === (data.week.find((d) => d.state === 'today') || {}).date;
  const liveTotal = todayLive ? f2(tonight.lineup_live) : '—';
  const bar = (week) => html`<div class="bar start">
    <${DayPicker} week=${week} day=${day} onDay=${pickDay} active=${view === 'day'} onActivate=${() => choose('day')}
      right=${(d) => (d.state === 'final' ? [d.scored === null ? '' : `${f2(d.scored)} pts`, '']
        : d.state === 'today' ? [`live ${liveTotal}`, 'you'] : [d.proj === null || d.proj === undefined ? '' : `proj ${f2(d.proj)}`, 'dim'])}
      unavailable=${(d) => (d.state === 'final' && !d.planned ? 'No plan was saved that day' : null)} />
    <button type="button" class=${`toggle ${view === 'season' ? 'on' : ''}`} aria-pressed=${view === 'season'}
      onClick=${() => choose('season')}>Season</button>
  </div>`;
  if (error) {
    if (error.status === 404 && asked !== 'today') {
      return html`<div class="bar start"><button type="button" class="btn small" onClick=${() => pickDay('today')}>Back to today</button></div>
        <div class="empty">${error.message}.</div>`;
    }
    return error.status === 404
      ? html`<${NoPlan} />`
      : html`<div class="error" role="alert">${error.message}</div>`;
  }
  if (!data) return html`<div class="empty">Loading…</div>`;
  const entry = (data.week || []).find((d) => (asked === 'today' ? d.state === 'today' : d.date === asked)) || { state: 'today' };
  const gone = entry.state === 'final';
  // Live points: today's from /tonight (once its day is today's), another day's come with the roster.
  const live = entry.state === 'today' ? (todayLive ? tonight.players : null) : data.players || null;
  const all = data.groups.flatMap((g) => g.rows);
  const lineup = all.filter((r) => r.where === 'lineup');
  // The strip follows the day: today its projection, live points and next puck; a day gone what
  // was projected, scored and how many of the lineup played; a day ahead its projection and first puck.
  const firstPuck = () => {
    const starts = lineup.map((r) => live && live[r.player_id] && live[r.player_id].game)
      .filter((g) => g && g.start_utc).map((g) => g.start_utc).sort();
    return starts.length ? puckTime(starts[0].replace('T', ' ').replace('Z', '')) : data.next_puck ? puckTime(data.next_puck) : '—';
  };
  const played = lineup.filter((r) => live && live[r.player_id] && live[r.player_id].line).length;
  const sum = entry.state === 'today'
    ? [['Proj tonight', f2(data.proj_tonight)], ['Live', liveTotal], ['Next puck', data.next_puck ? puckTime(data.next_puck) : '—']]
    : gone
      ? [['Projected', f2(data.proj_tonight)], ['Scored', entry.scored === null || entry.scored === undefined ? '—' : f2(entry.scored)], ['Played', `${played} of ${lineup.length}`]]
      : [[`Proj ${shortDay(entry.date)}`, f2(data.proj_tonight)], ['Live', '—'], ['First puck', firstPuck()]];
  // Season: the whole roster in two groups, as the design, so a sort ranks everyone you hold.
  const groups = view === 'season'
    ? [{ name: 'Skaters', goalie: false, rows: all.filter((r) => !r.goalie) },
       { name: 'Goalies', goalie: true, rows: all.filter((r) => r.goalie) }].filter((g) => g.rows.length)
    : data.groups;
  const span = Math.max(...groups.map((g) => heads(view, g.goalie, groupColumns(g, data.columns), gone).length));
  return html`
    <div class="sum">
      ${sum.map(([k, v], i) => html`<div><span class="k">${k}</span><span class=${`v ${i === 1 ? 'you' : ''}`}>${v}</span></div>`)}
    </div>
    ${bar(data.week || [])}
    ${liveError && entry.state === 'today' && html`<div class="error" role="alert">Live points: ${liveError}</div>`}
    ${view === 'day' && !data.games_today && html`<div class="empty">No NHL games on ${data.day}: the roster, no lineup to set.</div>`}
    ${view === 'day' && entry.state === 'today' && data.empty_slots > 0 && data.games_today
      && html`<div class="error">${data.empty_slots} empty lineup slot${data.empty_slots > 1 ? 's' : ''} tonight</div>`}
    ${view === 'day' && !wide
      ? html`<div class="tn-list">${groups.map((g) => html`<${DayGroup} key=${g.name} group=${g} columns=${data.columns} live=${live} />`)}</div>`
      : html`<div class="table-scroll"><table class=${`roster ${view}`}>
      ${groups.map((g) => html`<${Group} key=${g.name} group=${g} view=${view} columns=${data.columns} span=${span}
        live=${live} gone=${gone} sort=${sort} onSort=${sortBy} />`)}
    </table></div>`}`;
}
