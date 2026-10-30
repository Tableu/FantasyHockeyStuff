// The Players screen (redesign plan app-redesign.md, step 2): every free agent from GET /players
// (the newest plan) in one table -- FP, FP/g, ROS and GP for everyone, the skater columns, then the
// goalie columns after a line -- best first by ROS; tap a header to sort by it (again to flip).
// Filter: positions, status (free agent or waivers), the nights left this week he plays, teams;
// each one picked shows as a chip under the bar, tapped to take it off. On a phone the filter is a
// bottom sheet; on a PC (`wide`) a panel beside the table.
import { html, useState, useEffect } from './vendor/htm-preact-3.1.1.mjs';
import { api } from './api.js';
import { NoPlan, Photo, useEscape } from './layout.js';

const POSITIONS = ['C', 'LW', 'RW', 'D', 'G'];
const STATUS = { all: 'Any', fa: 'Free agent', w: 'Waivers' };
const TEAMS = ['ANA', 'BOS', 'BUF', 'CAR', 'CBJ', 'CGY', 'CHI', 'COL', 'DAL', 'DET', 'EDM', 'FLA', 'LAK', 'MIN', 'MTL', 'NJD',
  'NSH', 'NYI', 'NYR', 'OTT', 'PHI', 'PIT', 'SEA', 'SJS', 'STL', 'TBL', 'TOR', 'UTA', 'VAN', 'VGK', 'WPG', 'WSH'];
const FLAG_CLASS = { DTD: 'warn', GTD: 'warn', Q: 'warn', O: 'bad', OUT: 'bad', IR: 'bad', SUSP: 'bad' };
const NO_FILTER = { pos: [], teams: [], status: 'all', days: [] };

const store = {
  get(key) { try { return localStorage.getItem(key); } catch (_) { return null; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch (_) { /* private window */ } },
};

const f2 = (x) => (x === null || x === undefined ? '' : Number(x).toFixed(2));
const svPct = (s) => (s.sv_pct ? s.sv_pct.toFixed(3).replace(/^0/, '') : '');

// "2026-10-31" -> "Sat Oct 31" (a date, not a moment).
function dayLabel(iso) {
  const [y, m, d] = iso.split('-').map(Number);
  const date = new Date(y, m - 1, d);
  return `${date.toLocaleDateString('en-US', { weekday: 'short' })} ${date.toLocaleDateString('en-US', { month: 'short' })} ${d}`;
}

// The table's columns: [key, label, class, what it stands for]. A player's cells in the other
// kind's columns stay blank.
function columnsOf(columns) {
  return [
    ['fp', 'FP', 'f', 'Fantasy points this season'], ['fp_per_game', 'FP/g', 'f', 'Fantasy points per game this season'],
    ['ros', 'ROS', 'f', 'Projected fantasy points for the rest of the season'], ['gp', 'GP', '', 'Games played'],
    ...columns.skater.map(([key, label]) => [key, label, '', '']),
    ...columns.goalie.map(([key, label], i) => [key, label, i ? '' : 'gk', '']),
    ['sv_pct', 'SV%', '', 'Save percentage'],
  ];
}

const SKATER_ONLY = (columns) => new Set(columns.skater.map(([key]) => key));
const GOALIE_ONLY = (columns) => new Set([...columns.goalie.map(([key]) => key), 'sv_pct']);

function value(row, key, columns) {
  if (key === 'ros') return row.ros;
  if (row.goalie ? SKATER_ONLY(columns).has(key) : GOALIE_ONLY(columns).has(key)) return null;
  const v = (row.season || {})[key];
  return v === undefined ? null : v;
}

function cellText(row, key, columns) {
  const v = value(row, key, columns);
  if (v === null) return '';
  if (key === 'fp' || key === 'fp_per_game') return f2(v);
  if (key === 'ros') return String(Math.round(v));
  if (key === 'sv_pct') return svPct(row.season);
  return String(v);
}

// Whether a row passes the filters; `skip` leaves one filter out (a sheet option's own count).
function keeps(row, f, skip) {
  return (skip === 'pos' || !f.pos.length || row.positions.split('/').some((p) => f.pos.includes(p)))
    && (skip === 'team' || !f.teams.length || f.teams.includes(row.team))
    && (f.status === 'all' || (f.status === 'w' ? row.waivers : !row.waivers))
    && (skip === 'day' || !f.days.length || row.nights.some((d) => f.days.includes(d)));
}

function FilterPanel({ data, filter, setFilter, count, onClose, wide }) {
  const toggle = (key, v) => setFilter({ ...filter, [key]: filter[key].includes(v) ? filter[key].filter((x) => x !== v) : [...filter[key], v] });
  const any = filter.pos.length || filter.teams.length || filter.days.length || filter.status !== 'all';
  return html`<section class=${wide ? 'dock fa-filter' : 'sheet fa-sheet fa-filter'} role="dialog" aria-label="Filter free agents">
    <div class="fa-head">
      <span class="title">Filter</span>
      <span class="acts">
        <button type="button" class=${`link-btn ${any ? 'you' : 'off'}`} onClick=${() => setFilter(NO_FILTER)}>Reset</button>
        <button type="button" class="icon-btn" onClick=${onClose} aria-label="Close"><svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M6 6l12 12M18 6 6 18"></path></svg></button>
      </span>
    </div>
    <div class="fa-body">
      <h3 class="sh-sec">Position<span>any of</span></h3>
      <div class="opts five" role="group" aria-label="Position">
        ${POSITIONS.map((p) => html`<button type="button" class="opt" aria-pressed=${filter.pos.includes(p)} onClick=${() => toggle('pos', p)}>${p}</button>`)}
      </div>
      <h3 class="sh-sec">Status</h3>
      <div class="opts three" role="group" aria-label="Status">
        ${Object.entries(STATUS).map(([v, label]) => html`<button type="button" class="opt" aria-pressed=${filter.status === v}
          onClick=${() => setFilter({ ...filter, status: v })}>${label}</button>`)}
      </div>
      <h3 class="sh-sec">Plays on<span>${data.days.length} night${data.days.length === 1 ? '' : 's'} left this week · any of</span></h3>
      <div class="checks" role="group" aria-label="Plays on">
        ${data.days.map((d, i) => {
          const on = filter.days.includes(d.date);
          const n = data.rows.filter((r) => r.nights.includes(d.date) && keeps(r, filter, 'day')).length;
          return html`<button type="button" class="check" aria-pressed=${on} onClick=${() => toggle('days', d.date)}>
            <span class="lbl"><span class=${`box ${on ? 'on' : ''}`} aria-hidden="true">${on && html`<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12 5 5 9-10"></path></svg>`}</span>
              <span>${dayLabel(d.date)}${i === 0 && d.date === data.day ? html` <span class="dim">today</span>` : ''}</span></span>
            <span class="n">${d.games === null || d.games === undefined ? '' : `${d.games} games · `}${n} FA</span>
          </button>`;
        })}
      </div>
      <h3 class="sh-sec">Team<span>${filter.teams.length ? `${filter.teams.length} picked` : 'any'}</span></h3>
      <div class="opts five teams" role="group" aria-label="Team">
        ${TEAMS.map((t) => {
          const n = data.rows.filter((r) => r.team === t && keeps(r, filter, 'team')).length;
          return html`<button type="button" class=${`opt tm ${n ? '' : 'none'}`} aria-pressed=${filter.teams.includes(t)}
            aria-label=${`${t}, ${n} free agents`} onClick=${() => toggle('teams', t)}>${t}</button>`;
        })}
      </div>
    </div>
    <div class="fa-foot"><button type="button" class="btn primary-light" onClick=${onClose}>Show ${count} players</button></div>
  </section>`;
}

export function Players({ league, version, wide }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [filter, setFilter] = useState(NO_FILTER);
  const [open, setOpen] = useState(false);
  // The sort: {key, desc}, ROS high to low until a header is tapped; kept on this device.
  const [sort, setSort] = useState(() => {
    try { return JSON.parse(store.get('fh.players.sort')) || { key: 'ros', desc: true }; } catch (_) { return { key: 'ros', desc: true }; }
  });
  useEscape(open, () => setOpen(false));

  useEffect(() => {
    let gone = false;
    setError(null);
    api.players(league)
      .then((got) => { if (!gone) setData(got); })
      .catch((e) => { if (!gone) { setData(null); setError(e); } });
    return () => { gone = true; };
  }, [league, version]);

  if (error) return error.status === 404 ? html`<${NoPlan} />` : html`<div class="error" role="alert">${error.message}</div>`;
  if (!data) return html`<div class="empty">Loading…</div>`;

  const cols = columnsOf(data.columns);
  // A new column sorts high to low, a second tap flips it. Blanks (a goalie in a skater column)
  // always last; ties by ROS.
  const sortBy = (key) => {
    const next = sort.key !== key ? { key, desc: true } : { key, desc: !sort.desc };
    setSort(next);
    store.set('fh.players.sort', JSON.stringify(next));
  };
  const shown = data.rows.filter((r) => keeps(r, filter)).sort((a, b) => {
    const x = value(a, sort.key, data.columns), y = value(b, sort.key, data.columns);
    if (x === null && y === null) return (b.ros || 0) - (a.ros || 0);
    if (x === null) return 1;
    if (y === null) return -1;
    return x === y ? (b.ros || 0) - (a.ros || 0) : sort.desc ? y - x : x - y;
  });
  const toggle = (key, v) => setFilter({ ...filter, [key]: filter[key].filter((x) => x !== v) });
  const chips = [
    ...filter.pos.map((v) => [v, () => toggle('pos', v)]),
    ...filter.teams.map((v) => [v, () => toggle('teams', v)]),
    ...(filter.status !== 'all' ? [[STATUS[filter.status], () => setFilter({ ...filter, status: 'all' })]] : []),
    ...data.days.filter((d) => filter.days.includes(d.date)).map((d) => [`Plays ${dayLabel(d.date).slice(0, 3)}`, () => toggle('days', d.date)]),
  ];
  const sortCol = cols.find((c) => c[0] === sort.key) || cols[2];
  const table = html`
    <div class="fa-bar">
      <div class="fa-row">
        <button type="button" class=${`fbtn ${chips.length ? 'on' : ''}`} onClick=${() => setOpen(!open)} aria-haspopup="dialog" aria-expanded=${open}>
          <svg aria-hidden="true" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 5h18l-7 8.5V19l-4 2v-7.5z"></path></svg>Filter
          ${chips.length > 0 && html`<span class="fbadge">${chips.length}</span>`}
        </button>
        <div class="chips">
          ${chips.map(([label, remove]) => html`<button type="button" class="fchip" onClick=${remove} aria-label=${`Remove filter ${label}`}>${label}<svg aria-hidden="true" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 6l12 12M18 6 6 18"></path></svg></button>`)}
          ${!chips.length && html`<span class="none">All teams · all positions</span>`}
        </div>
      </div>
      <span class="fa-note">${shown.length} free agent${shown.length === 1 ? '' : 's'} · best first by <b class="you">${sortCol[1]}${sort.desc ? '' : ' (lowest first)'}</b><span class="warn"> · W</span> = on waivers</span>
    </div>
    <div class="table-scroll"><table class="roster fa">
      <tbody>
        <tr class="sub">
          <th scope="col" class="stick">Player</th>
          ${cols.map(([key, label, cls, title]) => {
            const on = sort.key === key;
            return html`<th scope="col" class=${cls} title=${title || undefined} style=${key === 'fp_per_game' && !on ? 'color: var(--text)' : ''}
              aria-sort=${on ? (sort.desc ? 'descending' : 'ascending') : undefined}>
              <button type="button" class=${`sort ${on ? 'on' : ''}`} onClick=${() => sortBy(key)} aria-label=${`Sort by ${title || label}`}>${label}${on ? (sort.desc ? ' ▾' : ' ▴') : ''}</button></th>`;
          })}
        </tr>
        ${shown.map((r) => html`<tr>
          <th scope="row" class="stick">
            <${Photo} src=${r.photo} name=${r.name} size=${34} />
            <span class="pname-txt" title=${r.player}>${r.name}</span><span class="elig" title="Eligible positions">${r.positions}</span>
            ${r.waivers && html` <span class="flag warn" title="On waivers: a claim, not an add">W</span>`}
            ${r.flag && html` <span class=${`flag ${FLAG_CLASS[r.flag] || 'warn'}`}>${r.flag}</span>`}
          </th>
          ${cols.map(([key, , cls]) => html`<td class=${cls} style=${key === 'fp_per_game' ? 'font-weight: 600' : ''}>${cellText(r, key, data.columns)}</td>`)}
        </tr>`)}
        ${!shown.length && html`<tr><td colspan=${cols.length + 1} class="txt fa-empty"><div>No free agents match these filters.
          <button type="button" class="link-btn you" onClick=${() => setFilter(NO_FILTER)}>Clear filters</button></div></td></tr>`}
      </tbody>
    </table></div>`;
  const panel = open && html`<${FilterPanel} data=${data} filter=${filter} setFilter=${setFilter} count=${shown.length}
    onClose=${() => setOpen(false)} wide=${wide} />`;
  if (wide) return html`<div class="split"><div class="split-main">${table}</div>${panel}</div>`;
  return html`${table}${open && html`<div class="scrim" aria-hidden="true" onClick=${() => setOpen(false)}></div>`}${panel}`;
}
