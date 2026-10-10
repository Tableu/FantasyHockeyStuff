// The Roster screen (plan P1): your lineup as it stands, bench and IR from GET /roster (the newest
// plan), with tonight's games and live points from GET /tonight. Tonight / Season switch the
// columns (Season: tap a header to sort); tap a name for what the plan says about him.
import { html, useState, useEffect } from './vendor/htm-preact-3.1.1.mjs';
import { api } from './api.js';

const TONIGHT_EVERY_MS = 20000;     // the server's NHL feed cache is about 20 s
const STARTED = new Set(['LIVE', 'CRIT', 'OFF', 'FINAL']);
const FLAG_CLASS = { DTD: 'warn', GTD: 'warn', Q: 'warn', O: 'bad', OUT: 'bad', IR: 'bad', SUSP: 'bad' };
const ORD = { 1: '1st', 2: '2nd', 3: '3rd' };

const store = {
  get(key) { try { return localStorage.getItem(key); } catch (_) { return null; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch (_) { /* private window */ } },
};

const f1 = (x) => (x === null || x === undefined ? '' : Number(x).toFixed(1));
const f2 = (x) => (x === null || x === undefined ? '' : Number(x).toFixed(2));

// The plan's puck times are UTC without a zone ("2026-10-10 23:00:00").
function puckTime(utc) {
  if (!utc) return '';
  const t = new Date(utc.replace(' ', 'T') + 'Z');
  const h = t.getHours() % 12 || 12;
  const m = String(t.getMinutes()).padStart(2, '0');
  return `${h}:${m} ${t.getHours() < 12 ? 'AM' : 'PM'}`;
}

function gameLabel(row, game) {
  const team = row.team || '';
  if (!game) return row.puck_utc ? `${team} ${puckTime(row.puck_utc)}` : `${team} —`;
  const vs = `${team} ${game.home ? 'v ' : '@'}${game.vs}`;
  if (game.state === 'FUT' || game.state === 'PRE') return `${vs} ${puckTime(game.start_utc.replace('T', ' ').replace('Z', ''))}`;
  if (game.state === 'OFF' || game.state === 'FINAL') return `${vs} F ${game.score}–${game.vs_score}`;
  if (game.intermission) return `${vs} INT`;
  const period = game.period_type === 'OT' ? 'OT' : game.period_type === 'SO' ? 'SO' : ORD[game.period] || `P${game.period}`;
  return `${vs} ${period}`;
}

function tonightCells(row, live, goalie) {
  const game = live && live.game;
  const line = live && live.line;
  const started = (game && STARTED.has(game.state)) || row.locked;
  let prob = '—', probWarn = false;
  if (row.p_plays !== null && row.p_plays !== undefined) {
    // Started: ✓ once he's in it -- a goalie who sat (no box line, or 0:00) didn't play.
    if (started && game) prob = goalie && (!line || !line.toi || line.toi === '00:00') ? (line ? 'DNP' : '…') : '✓';
    else {
      prob = String(Math.round(row.p_plays * 100));
      probWarn = row.p_plays < 0.9;
    }
  }
  const cells = [
    { text: f1(row.xpts), cls: 'f', weight: 600 },
    { text: line ? f1(line.fantasy) : '', cls: 'f you', weight: 600 },
    { text: prob, cls: `f ${probWarn ? 'warn' : 'dim'}` },
    { text: gameLabel(row, game), cls: 'txt dim2' },
  ];
  const stats = !line ? (goalie ? ['', '', '', '', ''] : ['', '', '', '', '', ''])
    : goalie
      ? [line.shots_against, line.saves, line.goals_against,
         line.shots_against ? (line.saves / line.shots_against).toFixed(3).replace(/^0/, '') : '', line.toi]
      : [line.goals, line.assists, line.shots, line.hits, line.blocks, line.toi];
  return cells.concat(stats.map((v) => ({ text: v === undefined || v === null ? '' : String(v) })));
}

function seasonCells(row, columns) {
  const s = row.season || {};
  const cells = [
    { text: s.gp ? f1(s.fp) : '', cls: 'f' },
    { text: s.fp_per_game === null ? '' : f2(s.fp_per_game), cls: 'f', weight: 600 },
    { text: row.ros === null || row.ros === undefined ? '' : Math.round(row.ros), cls: 'f' },
  ];
  for (const [key] of columns) cells.push({ text: s[key] === undefined || s[key] === null ? '' : String(s[key]) });
  if (row.goalie) cells.push({ text: s.sv_pct ? s.sv_pct.toFixed(3).replace(/^0/, '') : '' });
  return cells;
}

function heads(view, goalie, columns) {
  const F = (label, strong) => ({ label, cls: 'f', strong });
  if (view === 'tonight') {
    return [F('xPts', true), F('Live'), F('P%'), { label: 'Game', cls: 'txt' },
      ...(goalie ? ['SA', 'SV', 'GA', 'SV%', 'TOI'] : ['G', 'A', 'SOG', 'HIT', 'BLK', 'TOI']).map((label) => ({ label }))];
  }
  return [{ ...F('FP'), key: 'fp' }, { ...F('FP/g', true), key: 'fp_per_game' }, { ...F('ROS'), key: 'ros' },
    ...columns.map(([key, label]) => ({ label, key })), ...(goalie ? [{ label: 'SV%', key: 'sv_pct' }] : [])];
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

function Group({ group, view, columns, live, open, onToggle, sort, onSort }) {
  const cols = heads(view, group.goalie, group.goalie ? columns.goalie : columns.skater);
  const rows = view === 'season' ? sorted(group.rows, sort) : group.rows;
  const head = (c) => {
    if (!c.key) return c.label;
    const on = sort && sort.key === c.key;
    return html`<button type="button" class=${`sort ${on ? 'on' : ''}`} onClick=${() => onSort(c.key)}
      aria-label=${`Sort by ${c.label}`}>${c.label}${on ? (sort.desc ? ' ▾' : ' ▴') : ''}</button>`;
  };
  return html`<table class="roster"><tbody>
    <tr class="sub">
      <th scope="colgroup" class="stick">${group.name}</th>
      ${cols.map((c) => html`<th scope="col" class=${c.cls || ''} style=${c.strong ? 'color: var(--text)' : ''}
        aria-sort=${c.key && sort && sort.key === c.key ? (sort.desc ? 'descending' : 'ascending') : undefined}>${head(c)}</th>`)}
    </tr>
    ${rows.map((row) => {
      const cells = view === 'tonight'
        ? tonightCells(row, live && live[row.player_id], group.goalie)
        : seasonCells(row, group.goalie ? columns.goalie : columns.skater);
      const isOpen = open === row.player_id && row.notes.length > 0;
      return html`
        <tr class=${isOpen ? 'sel' : ''}>
          <th scope="row" class="stick">
            <span class="slot">${row.slot}</span>
            <button type="button" class=${`pname ${FLAG_CLASS[row.flag] || ''}`} aria-expanded=${isOpen}
              onClick=${() => onToggle(row.player_id)} title=${row.player}>${row.name}</button>
            ${row.flag && html` <span class=${`flag ${FLAG_CLASS[row.flag] || 'warn'}`}>${row.flag}</span>`}
          </th>
          ${cells.map((c) => html`<td class=${c.cls || ''} style=${c.weight ? `font-weight: ${c.weight}` : ''}>${c.text}</td>`)}
        </tr>
        ${isOpen && html`<tr class="note"><td colspan=${cols.length + 1}><span>${row.player} · ${row.positions}: ${row.notes.join(' · ')}</span></td></tr>`}`;
    })}
  </tbody></table>`;
}

export function Roster({ league, version }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [tonight, setTonight] = useState(null);
  const [liveError, setLiveError] = useState(null);
  const [view, setView] = useState(() => store.get('fh.roster.view') || 'tonight');
  const [open, setOpen] = useState(null);
  // The Season sort: {key, desc}, or null for the roster's order; kept on this phone.
  const [sort, setSort] = useState(() => {
    try { return JSON.parse(store.get('fh.roster.sort')) || null; } catch (_) { return null; }
  });

  useEffect(() => {
    let gone = false;
    setError(null);
    api.roster(league)
      .then((got) => { if (!gone) setData(got); })
      .catch((e) => { if (!gone) { setData(null); setError(e); } });
    return () => { gone = true; };
  }, [league, version]);

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

  if (error) {
    return error.status === 404
      ? html`<div class="empty">No plan saved for today yet. Tap Quick to make one.</div>`
      : html`<div class="error" role="alert">${error.message}</div>`;
  }
  if (!data) return html`<div class="empty">Loading…</div>`;
  // Live points belong to today's games; once the plan is tomorrow's, they don't apply to it.
  const live = tonight && tonight.date === data.day ? tonight.players : null;
  const liveTotal = live ? f1(tonight.lineup_live) : '—';
  // Season: the whole roster in two groups, as the design, so a sort ranks everyone you hold.
  const all = data.groups.flatMap((g) => g.rows);
  const groups = view === 'season'
    ? [{ name: 'Skaters', goalie: false, rows: all.filter((r) => !r.goalie) },
       { name: 'Goalies', goalie: true, rows: all.filter((r) => r.goalie) }].filter((g) => g.rows.length)
    : data.groups;
  return html`
    <div class="sum">
      <div><span class="k">Proj tonight</span><span class="v">${f1(data.proj_tonight)}</span></div>
      <div><span class="k">Live</span><span class="v you">${liveTotal}</span></div>
      <div><span class="k">Next puck</span><span class="v">${data.next_puck ? puckTime(data.next_puck) : '—'}</span></div>
    </div>
    <div class="bar">
      <div class="seg" role="group" aria-label="Columns">
        <button type="button" aria-pressed=${view === 'tonight'} onClick=${() => choose('tonight')}>Tonight</button>
        <button type="button" aria-pressed=${view === 'season'} onClick=${() => choose('season')}>Season</button>
      </div>
    </div>
    ${liveError && html`<div class="error" role="alert">Live points: ${liveError}</div>`}
    ${!data.games_today && html`<div class="empty">No NHL games on ${data.day}: the roster, no lineup to set.</div>`}
    ${data.empty_slots > 0 && data.games_today && html`<div class="error">${data.empty_slots} empty lineup slot${data.empty_slots > 1 ? 's' : ''} tonight</div>`}
    <div class="table-scroll">
      ${groups.map((g) => html`<${Group} key=${g.name} group=${g} view=${view} columns=${data.columns}
        live=${live} open=${open} onToggle=${(id) => setOpen(open === id ? null : id)}
        sort=${sort} onSort=${sortBy} />`)}
    </div>`;
}
