// The Roster screen (plan P1; redesign plan app-redesign.md, step 1): your lineup, bench and IR
// from GET /roster as the Season table -- each player's season in the league's scoring (tap a
// header to sort; Per game shows the stats as rates). The day's view (each day's lineup, its
// games and live points) is the Matchup's Players view since 2026-11-08 (the user).
import { html, useState, useEffect } from './vendor/htm-preact-3.1.1.mjs';
import { api } from './api.js';
import { NoPlan, Photo, posLabel } from './layout.js';

const FLAG_CLASS = { DTD: 'warn', GTD: 'warn', Q: 'warn', O: 'bad', OUT: 'bad', IR: 'bad', SUSP: 'bad' };

const store = {
  get(key) { try { return localStorage.getItem(key); } catch (_) { return null; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch (_) { /* private window */ } },
};

const f2 = (x) => (x === null || x === undefined ? '' : Number(x).toFixed(2));

// A Season column's value, the league's scoring stats a game when `perGame`; FP, FP/g, ROS, GP and
// SV% stay as they are.
const NOT_A_COUNT = new Set(['fp', 'fp_per_game', 'ros', 'gp', 'sv_pct']);
function seasonValue(row, key, perGame) {
  if (key === 'ros') return row.ros;
  const s = row.season || {};
  const v = s[key];
  return perGame && s.gp && v !== undefined && v !== null && !NOT_A_COUNT.has(key) ? v / s.gp : v;
}

function seasonCells(row, columns, perGame) {
  const s = row.season || {};
  const cells = [
    { text: s.gp ? f2(s.fp) : '', cls: 'f' },
    { text: s.fp_per_game === null ? '' : f2(s.fp_per_game), cls: 'f', weight: 600 },
    { text: row.ros === null || row.ros === undefined ? '' : Math.round(row.ros), cls: 'f' },
  ];
  for (const [key] of columns) {
    const v = seasonValue(row, key, perGame);
    cells.push({ text: v === undefined || v === null ? '' : perGame && !NOT_A_COUNT.has(key) ? f2(v) : String(v) });
  }
  if (row.goalie) cells.push({ text: s.sv_pct ? s.sv_pct.toFixed(3).replace(/^0/, '') : '' });
  return cells;
}

// What a header's abbreviation stands for, shown on hover.
const TITLES = {
  FP: 'Fantasy points this season', 'FP/g': 'Fantasy points per game this season',
  ROS: 'Projected fantasy points for the rest of the season',
};

function heads(goalie, columns) {
  const F = (label, strong) => ({ label, cls: 'f', strong });
  return [{ ...F('FP'), key: 'fp' }, { ...F('FP/g', true), key: 'fp_per_game' }, { ...F('ROS'), key: 'ros' },
    ...columns.map(([key, label]) => ({ label, key })), ...(goalie ? [{ label: 'SV%', key: 'sv_pct' }] : [])]
    .map((c) => ({ ...c, title: TITLES[c.label] }));
}

// Rows by a Season column as shown, missing values last either way; ties keep the roster's order.
function sorted(rows, sort, perGame) {
  if (!sort) return rows;
  const dir = sort.desc ? -1 : 1;
  return rows.map((row, i) => [row, i]).sort(([a, i], [b, j]) => {
    const x = seasonValue(a, sort.key, perGame), y = seasonValue(b, sort.key, perGame);
    const xm = x === null || x === undefined, ym = y === null || y === undefined;
    if (xm || ym) return xm && ym ? i - j : xm ? 1 : -1;
    return x === y ? i - j : (x < y ? -1 : 1) * dir;
  }).map(([row]) => row);
}

const groupColumns = (group, columns) => (group.goalie ? columns.goalie : columns.skater);

// Name, eligible positions and flag.
function Who({ row }) {
  return html`<span class=${`pname ${FLAG_CLASS[row.flag] || ''}`} title=${row.player}>${row.name}</span><span class="elig" title="Eligible positions">${posLabel(row.positions)}</span>
    ${row.flag && html` <span class=${`flag ${FLAG_CLASS[row.flag] || 'warn'}`}>${row.flag}</span>`}`;
}

// One group's rows in the Season table; `span` is the widest group's column count, so a group with
// fewer columns (the goalies) is padded out to the table's edge.
function Group({ group, columns, span, sort, onSort, perGame }) {
  const cols = heads(group.goalie, groupColumns(group, columns));
  const pad = Array.from({ length: Math.max(0, span - cols.length) });
  const rows = sorted(group.rows, sort, perGame);
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
      const cells = seasonCells(row, groupColumns(group, columns), perGame);
      return html`
        <tr>
          <th scope="row" class="stick">
            <${Photo} src=${row.photo} name=${row.name} size=${30} />
            <${Who} row=${row} />
          </th>
          ${cells.map((c) => html`<td class=${c.cls || ''} style=${c.weight ? `font-weight: ${c.weight}` : ''}>${c.text}</td>`)}
          ${pad.map(() => html`<td></td>`)}
        </tr>`;
    })}
  </tbody>`;
}

export function Roster({ league, version }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  // The sort: {key, desc}, or null for the roster's order; kept on this device.
  const [sort, setSort] = useState(() => {
    try { return JSON.parse(store.get('fh.roster.sort')) || null; } catch (_) { return null; }
  });
  // The stats as totals or a game each; kept on this device.
  const [perGame, setPerGame] = useState(() => store.get('fh.roster.perGame') === '1');

  useEffect(() => {
    let gone = false;
    setError(null);
    api.roster(league)
      .then((got) => { if (!gone) setData(got); })
      .catch((e) => { if (!gone) { setData(null); setError(e); } });
    return () => { gone = true; };
  }, [league, version]);

  const flipPerGame = () => { setPerGame(!perGame); store.set('fh.roster.perGame', perGame ? '0' : '1'); };
  // A column's first tap sorts it high to low, the second low to high, the third back to the roster.
  const sortBy = (key) => {
    const next = !sort || sort.key !== key ? { key, desc: true } : sort.desc ? { key, desc: false } : null;
    setSort(next);
    store.set('fh.roster.sort', JSON.stringify(next));
  };

  if (error) {
    return error.status === 404
      ? html`<${NoPlan} />`
      : html`<div class="error" role="alert">${error.message}</div>`;
  }
  if (!data) return html`<div class="empty">Loading…</div>`;
  // The whole roster in two groups, as the design, so a sort ranks everyone you hold.
  const all = data.groups.flatMap((g) => g.rows);
  const groups = [{ name: 'Skaters', goalie: false, rows: all.filter((r) => !r.goalie) },
    { name: 'Goalies', goalie: true, rows: all.filter((r) => r.goalie) }].filter((g) => g.rows.length);
  const span = Math.max(...groups.map((g) => heads(g.goalie, groupColumns(g, data.columns)).length));
  return html`
    <div class="bar start">
      <button type="button" class=${`toggle ${perGame ? 'on' : ''}`} aria-pressed=${perGame}
        title="Show the stats per game" onClick=${flipPerGame}>Per game</button>
    </div>
    <div class="table-scroll"><table class="roster season">
      ${groups.map((g) => html`<${Group} key=${g.name} group=${g} columns=${data.columns} span=${span}
        sort=${sort} onSort=${sortBy} perGame=${perGame} />`)}
    </table></div>`;
}
