// The Roster screen (plan P1): your lineup as it stands, bench and IR from GET /roster (the newest
// plan), with tonight's games and live points from GET /tonight. Tonight / Season switch the
// columns (Season: tap a header to sort); tap a name for what the plan says about him. Tonight on
// a phone is a card per player (the user, 2026-10-20): name, P% and game, xPts on the right,
// his box line and TOI as tags below, live points under xPts. On a PC (`wide`) the table
// has room for both: tonight's columns, then the season's, no switch.
import { html, useState, useEffect } from './vendor/htm-preact-3.1.1.mjs';
import { api } from './api.js';
import { NoPlan, Photo } from './layout.js';

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
// first -- before the puck drops, its time; no game data (tomorrow), the puck time alone.
function gameLine(row, game) {
  if (!game) return row.puck_utc ? puckTime(row.puck_utc) : '';
  const vs = `${game.home ? '' : '@'}${game.vs}`;
  if (!STARTED.has(game.state)) return `${vs} ${puckTime(game.start_utc.replace('T', ' ').replace('Z', ''))}`;
  return `${vs} ${game.score ?? 0}–${game.vs_score ?? 0}`;
}

// P%: his chance to play, or once his game has started ✓ -- a goalie who sat (no box line, or
// 0:00) didn't play.
function playChance(row, live, goalie) {
  const game = live && live.game;
  const line = live && live.line;
  const started = (game && STARTED.has(game.state)) || row.locked;
  if (row.p_plays === null || row.p_plays === undefined) return { prob: '—', probWarn: false };
  if (started && game) {
    return { prob: goalie && (!line || !line.toi || line.toi === '00:00') ? (line ? 'DNP' : '…') : '✓', probWarn: false };
  }
  return { prob: String(Math.round(row.p_plays * 100)), probWarn: row.p_plays < 0.9 };
}

function tonightCells(row, live, goalie) {
  const line = live && live.line;
  const { prob, probWarn } = playChance(row, live, goalie);
  const cells = [
    { text: f2(row.xpts), cls: 'f', weight: 600 },
    { text: line ? f2(line.fantasy) : '', cls: 'f you', weight: 600 },
    { text: prob, cls: `f ${probWarn ? 'warn' : 'dim'}` },
  ];
  // A goalie's five stats take the skaters' six columns (a blank before TOI), so the groups line up.
  const stats = !line ? ['', '', '', '', '', '']
    : goalie
      ? [line.shots_against, line.saves, line.goals_against,
         line.shots_against ? (line.saves / line.shots_against).toFixed(3).replace(/^0/, '') : '', '', line.toi]
      : [line.goals, line.assists, line.shots, line.hits, line.blocks, line.toi];
  return cells.concat(stats.map((v) => ({ text: v === undefined || v === null ? '' : String(v) })));
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
  xPts: 'Expected fantasy points tonight', Live: 'Fantasy points so far tonight', 'P%': 'Chance he plays tonight',
  FP: 'Fantasy points this season',
  'FP/g': 'Fantasy points per game this season', ROS: 'Projected fantasy points for the rest of the season',
};

function heads(view, goalie, columns) {
  const F = (label, strong) => ({ label, cls: 'f', strong });
  const tonight = () => [F('xPts', true), F('Live'), F('P%'),
    ...(goalie ? ['SA', 'SV', 'GA', 'SV%', '', 'TOI'] : ['G', 'A', 'SOG', 'HIT', 'BLK', 'TOI']).map((label) => ({ label }))];
  const season = () => [{ ...F('FP'), key: 'fp' }, { ...F('FP/g', true), key: 'fp_per_game' }, { ...F('ROS'), key: 'ros' },
    ...columns.map(([key, label]) => ({ label, key })), ...(goalie ? [{ label: 'SV%', key: 'sv_pct' }] : [])];
  const cols = view === 'tonight' ? tonight() : view === 'season' ? season()
    : [...tonight(), ...season().map((c, i) => (i ? c : { ...c, cls: `${c.cls} sep` }))];
  return cols.map((c) => ({ ...c, title: TITLES[c.label] }));
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

// The columns a group shows: skaters' and goalies' differ in number.
const groupCols = (group, view, columns) => heads(view, group.goalie, group.goalie ? columns.goalie : columns.skater);

// One group's rows in the shared table; `span` is the widest group's column count, so a group with
// fewer columns (the goalies) is padded out to the table's edge.
function Group({ group, view, columns, span, live, open, onToggle, sort, onSort }) {
  const cols = groupCols(group, view, columns);
  const pad = Array.from({ length: Math.max(0, span - cols.length) });
  const rows = view === 'tonight' ? group.rows : sorted(group.rows, sort);
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
      const tonightRow = () => tonightCells(row, live && live[row.player_id], group.goalie);
      const seasonRow = () => seasonCells(row, group.goalie ? columns.goalie : columns.skater);
      const cells = view === 'tonight' ? tonightRow() : view === 'season' ? seasonRow()
        : [...tonightRow(), ...seasonRow().map((c, i) => (i ? c : { ...c, cls: `${c.cls} sep` }))];
      const isOpen = open === row.player_id && row.notes.length > 0;
      return html`
        <tr class=${isOpen ? 'sel' : ''}>
          <th scope="row" class="stick">
            <span class="slot">${row.slot}</span>
            <${Photo} src=${row.photo} name=${row.name} size=${34} />
            <span class="who">
              <span><button type="button" class=${`pname ${FLAG_CLASS[row.flag] || ''}`} aria-expanded=${isOpen}
                onClick=${() => onToggle(row.player_id)} title=${row.player}>${row.name}</button>
              ${row.flag && html` <span class=${`flag ${FLAG_CLASS[row.flag] || 'warn'}`}>${row.flag}</span>`}</span>
              ${view !== 'season' && html`<span class="game">${gameLine(row, live && live[row.player_id] && live[row.player_id].game)}</span>`}
            </span>
          </th>
          ${cells.map((c) => html`<td class=${c.cls || ''} style=${c.weight ? `font-weight: ${c.weight}` : ''}>${c.text}</td>`)}
          ${pad.map(() => html`<td></td>`)}
        </tr>
        ${isOpen && html`<tr class="note"><td colspan=${Math.max(span, cols.length) + 1}><span>${row.player} · ${row.positions}: ${row.notes.join(' · ')}</span></td></tr>`}`;
    })}
  </tbody>`;
}

// His box line as tags: a skater's G A SOG HIT BLK, a goalie's SA SV GA SV%; then TOI.
function boxTags(line, goalie) {
  if (!line) return [];
  if (goalie) {
    return [['SA', line.shots_against], ['SV', line.saves], ['GA', line.goals_against],
      ['SV%', line.shots_against ? (line.saves / line.shots_against).toFixed(3).replace(/^0/, '') : ''], ['TOI', line.toi]];
  }
  return [['G', line.goals], ['A', line.assists], ['SOG', line.shots], ['HIT', line.hits], ['BLK', line.blocks], ['TOI', line.toi]];
}

// Tonight on a phone: one card per player, the group's name sticking at the top.
function TonightGroup({ group, live, open, onToggle }) {
  return html`<section class="tn" aria-label=${group.name}>
    <h3 class="tn-head">${group.name}<span class="cap">Expected points</span></h3>
    ${group.rows.map((row) => {
      const mine = live && live[row.player_id];
      const line = mine && mine.line;
      const { prob, probWarn } = playChance(row, mine, group.goalie);
      const tags = boxTags(line, group.goalie);
      const isOpen = open === row.player_id && row.notes.length > 0;
      return html`<div class=${`tn-row ${isOpen ? 'sel' : ''}`}>
        <span class="slot">${row.slot}</span>
        <${Photo} src=${row.photo} name=${row.name} size=${34} />
        <span class="who">
          <span><button type="button" class=${`pname ${FLAG_CLASS[row.flag] || ''}`} aria-expanded=${isOpen}
            onClick=${() => onToggle(row.player_id)} title=${row.player}>${row.name}</button>
          ${row.flag && html` <span class=${`flag ${FLAG_CLASS[row.flag] || 'warn'}`}>${row.flag}</span>`}
          <span class=${`p ${probWarn ? 'warn' : ''}`} title="Chance he plays tonight">${/^\d+$/.test(prob) ? `${prob}%` : prob}</span></span>
          <span class="game">${gameLine(row, mine && mine.game)}</span>
        </span>
        <span class="x" title="Expected fantasy points tonight">${f2(row.xpts)}</span>
        ${line && html`
          <span class="tags">${tags.map(([k, v]) => html`<span class=${`tag ${v ? '' : 'zero'}`}><span class="k">${k}</span> ${v ?? ''}</span>`)}</span>
          <span class="live" title="Fantasy points so far tonight">${f2(line.fantasy)}</span>`}
      </div>
      ${isOpen && html`<div class="tn-note">${row.player} · ${row.positions}: ${row.notes.join(' · ')}</div>`}`;
    })}
  </section>`;
}

export function Roster({ league, version, wide }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [tonight, setTonight] = useState(null);
  const [liveError, setLiveError] = useState(null);
  const [view, setView] = useState(() => store.get('fh.roster.view') || 'tonight');
  // Today's or tomorrow's plan: today unless you pick tomorrow (not kept: tomorrow goes stale).
  const [day, setDay] = useState('today');
  const [open, setOpen] = useState(null);
  // The Season sort: {key, desc}, or null for the roster's order; kept on this phone.
  const [sort, setSort] = useState(() => {
    try { return JSON.parse(store.get('fh.roster.sort')) || null; } catch (_) { return null; }
  });

  useEffect(() => {
    let gone = false;
    setError(null);
    api.roster(league, day)
      .then((got) => { if (!gone) setData(got); })
      .catch((e) => { if (!gone) { setData(null); setError(e); } });
    return () => { gone = true; };
  }, [league, version, day]);

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

  const choose = (v) => { setView(v); store.set('fh.roster.view', v); };
  // A column's first tap sorts it high to low, the second low to high, the third back to the roster.
  const sortBy = (key) => {
    const next = !sort || sort.key !== key ? { key, desc: true } : sort.desc ? { key, desc: false } : null;
    setSort(next);
    store.set('fh.roster.sort', JSON.stringify(next));
  };

  // Tomorrow is there once a plan for it is saved (the server makes one when today's last game starts).
  const dayBar = (days) => html`<div class="seg" role="group" aria-label="Day">
    <button type="button" aria-pressed=${day === 'today'} onClick=${() => setDay('today')}>Today</button>
    <button type="button" aria-pressed=${day === 'tomorrow'} disabled=${days && !days.tomorrow.planned && day !== 'tomorrow'}
      title=${days && !days.tomorrow.planned ? "No plan for tomorrow yet: one is made when today's last game starts" : undefined}
      onClick=${() => setDay('tomorrow')}>Tomorrow</button>
  </div>`;
  if (error) {
    if (error.status === 404 && day === 'tomorrow') {
      return html`<div class="bar">${dayBar(null)}</div><div class="empty">No plan for tomorrow yet: one is made when today's last game starts.</div>`;
    }
    return error.status === 404
      ? html`<${NoPlan} />`
      : html`<div class="error" role="alert">${error.message}</div>`;
  }
  if (!data) return html`<div class="empty">Loading…</div>`;
  // Live points belong to today's games; once the plan is tomorrow's, they don't apply to it.
  const live = tonight && tonight.date === data.day ? tonight.players : null;
  const shownView = wide ? 'both' : view;
  const liveTotal = live ? f2(tonight.lineup_live) : '—';
  // Season: the whole roster in two groups, as the design, so a sort ranks everyone you hold.
  const all = data.groups.flatMap((g) => g.rows);
  const groups = shownView === 'season'
    ? [{ name: 'Skaters', goalie: false, rows: all.filter((r) => !r.goalie) },
       { name: 'Goalies', goalie: true, rows: all.filter((r) => r.goalie) }].filter((g) => g.rows.length)
    : data.groups;
  const span = Math.max(...groups.map((g) => groupCols(g, shownView, data.columns).length));
  return html`
    <div class="sum">
      <div><span class="k">Proj ${day === 'tomorrow' ? 'tomorrow' : 'tonight'}</span><span class="v">${f2(data.proj_tonight)}</span></div>
      <div><span class="k">Live</span><span class="v you">${liveTotal}</span></div>
      <div><span class="k">Next puck</span><span class="v">${data.next_puck ? puckTime(data.next_puck) : '—'}</span></div>
    </div>
    <div class="bar">
      ${dayBar(data.days)}
      ${!wide && html`<div class="seg" role="group" aria-label="Columns">
        <button type="button" aria-pressed=${view === 'tonight'} onClick=${() => choose('tonight')}>${day === 'tomorrow' ? 'Tomorrow' : 'Tonight'}</button>
        <button type="button" aria-pressed=${view === 'season'} onClick=${() => choose('season')}>Season</button>
      </div>`}
    </div>
    ${liveError && html`<div class="error" role="alert">Live points: ${liveError}</div>`}
    ${!data.games_today && html`<div class="empty">No NHL games on ${data.day}: the roster, no lineup to set.</div>`}
    ${data.empty_slots > 0 && data.games_today && html`<div class="error">${data.empty_slots} empty lineup slot${data.empty_slots > 1 ? 's' : ''} tonight</div>`}
    ${shownView === 'tonight'
      ? html`<div class="tn-list">${groups.map((g) => html`<${TonightGroup} key=${g.name} group=${g} live=${live}
          open=${open} onToggle=${(id) => setOpen(open === id ? null : id)} />`)}</div>`
      : html`<div class="table-scroll"><table class="roster">
      ${groups.map((g) => html`<${Group} key=${g.name} group=${g} view=${shownView} columns=${data.columns} span=${span}
        live=${live} open=${open} onToggle=${(id) => setOpen(open === id ? null : id)}
        sort=${sort} onSort=${sortBy} />`)}
    </table></div>`}`;
}
