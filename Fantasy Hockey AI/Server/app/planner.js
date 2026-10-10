// The Weekly planner (plan P3): your week built from your picks, from GET /week (the server puts
// them in on plan A's workbench and solves each night's lineup, as the window does). Tap an open
// slot (+) or one of your pickups (⇄) for the picker: the free agents who would start that night,
// the plans' picks first (GET /week/picker), and who each may drop (GET /week/drops). Every change
// goes through POST /week/edit and the calendar comes back as it now stands.
import { html, useState, useEffect } from './vendor/htm-preact-3.1.1.mjs';
import { api } from './api.js';

const TEAM = /\s*\(([A-Z]{2,3})\)\s*$/;
const f1 = (x) => (x === null || x === undefined ? '' : Number(x).toFixed(1));
const sg = (x) => (x === null || x === undefined ? '' : (x > 0 ? '+' : x < 0 ? '−' : '') + Math.abs(x).toFixed(1));

function short(full) {
  if (!full) return '';
  const bare = full.replace(TEAM, '');
  const i = bare.indexOf(' ');
  return i > 0 ? `${bare[0]}. ${bare.slice(i + 1)}` : bare;
}
const teamOf = (full) => ((full || '').match(TEAM) || [])[1] || '';

function dayTitle(iso) {
  const [y, m, d] = iso.slice(0, 10).split('-').map(Number);
  const t = new Date(y, m - 1, d);
  return `${t.toLocaleDateString('en-US', { weekday: 'short' })} ${t.toLocaleDateString('en-US', { month: 'short' })} ${d}`;
}
const weekday = (iso) => dayTitle(iso).split(' ')[0];

function Sheet({ league, week, sheet, onClose, onDone }) {
  const [list, setList] = useState(null);
  const [error, setError] = useState(null);
  const [filter, setFilter] = useState('All');
  const [sel, setSel] = useState(null);
  const [drops, setDrops] = useState(null);
  const [drop, setDrop] = useState(undefined);
  const [busy, setBusy] = useState(false);
  const occupant = sheet.occupant;

  useEffect(() => {
    api.picker(league, week, sheet.day, occupant && occupant.player_id)
      .then(setList).catch((e) => setError(e.message));
  }, []);

  const choosePlayer = (f) => {
    setSel(f);
    setDrops(null);
    setDrop(undefined);
    api.drops(league, week, sheet.day, f.player_id, occupant && occupant.player_id)
      .then((got) => { setDrops(got); setDrop(got.selected); })
      .catch((e) => setError(e.message));
  };

  const add = async () => {
    if (!sel || !drops || !drops.drops.length) return;
    setBusy(true);
    try {
      const body = occupant
        ? { week, action: 'replace', day: sheet.day, add: sel.player_id, rental: occupant.player_id }
        : { week, action: 'pick', day: sheet.day, add: sel.player_id, drop: drop === undefined ? null : drop };
      onDone(await api.weekEdit(league, body));
    } catch (e) {
      setError(e.message);
      setBusy(false);
    }
  };

  const shown = list ? list.adds.filter((f) => filter === 'All' || f.positions.split('/').includes(filter)) : [];
  const counts = list && list.counts_from !== sheet.day ? ` (adds count from ${weekday(list.counts_from)})` : '';
  return html`
    <div class="scrim" onClick=${onClose}></div>
    <section class="sheet" role="dialog" aria-label=${`Add on ${dayTitle(sheet.day)}`}>
      <div class="sheet-head">
        <span class="title">${dayTitle(sheet.day)} · ${sheet.slot} slot</span>
        <span class="sub">${occupant ? `Replacing ${short(occupant.player)} from this night` : 'Open'}${counts}.
          Free agents who would start that night: the plans' picks first, then the most points that night. Week: the lineup points he adds this week.</span>
      </div>
      ${error && html`<div class="error" role="alert">${error}</div>`}
      ${list && list.positions.length > 1 && html`
        <div class="filters"><span>Position</span>
          <div class="seg" role="group" aria-label="Position filter">
            ${['All', ...list.positions].map((q) => html`<button type="button" aria-pressed=${filter === q} onClick=${() => setFilter(q)}>${q}</button>`)}
          </div></div>`}
      <div class="adds">
        ${!list ? html`<div class="empty">Loading…</div>` : !shown.length
          ? html`<p class="foot">No free agent who plays that night would start.</p>`
          : html`<table class="list fixed">
            <thead><tr><th scope="col" style="width: 54px">Plans</th><th scope="col">Player</th>
              <th scope="col" class="n" style="width: 44px">Pts</th><th scope="col" class="n" style="width: 50px">Week</th>
              <th scope="col" class="n" style="width: 48px">Left</th></tr></thead>
            <tbody>${shown.map((f) => html`
              <tr class=${sel && sel.player_id === f.player_id ? 'sel' : f.plans ? 'mine' : ''}>
                <td class="state">${f.plans}</td>
                <td><button type="button" class="link" aria-pressed=${!!sel && sel.player_id === f.player_id}
                  onClick=${() => choosePlayer(f)}>${short(f.player)}${f.status && f.status !== 'ACTIVE' ? ` [${f.status}]` : ''}${' '}<span class="meta">${f.positions} · ${teamOf(f.player)}</span></button></td>
                <td class="n">${f1(f.pts)}</td><td class="n good">${sg(f.gain)}</td><td class="n dim">${f.games}</td>
              </tr>`)}</tbody></table>`}
      </div>
      <div class="drops">
        <div class="drops-head">${sel ? html`Drop for ${short(sel.player)} <span>· week gain with each</span>` : 'Pick a player for his drops'}</div>
        <div role="radiogroup" aria-label="Drop" class="drop-list">
          ${sel && !drops && html`<p class="foot">…</p>`}
          ${drops && !drops.drops.length && html`<p class="foot">No drop fits: no moves left — take a pick out.</p>`}
          ${drops && drops.drops.map((d) => html`
            <button type="button" role="radio" aria-checked=${d.player_id === drop} onClick=${() => setDrop(d.player_id)}>
              <span class="dot"></span>
              <span class="who">${d.player_id === null ? 'Open roster spot' : short(d.player)}${' '}<span class="meta">${d.player_id === null ? 'nobody dropped' : teamOf(d.player)}${sel && sel.plan_drop === d.player_id && d.player_id !== null ? " · plan's drop" : ''}</span></span>
              <span class="good mono">${sg(d.gain)}</span>
            </button>`)}
        </div>
      </div>
      <div class="sheet-foot">
        <button type="button" class="btn" onClick=${onClose}>Cancel</button>
        <button type="button" class="btn primary" disabled=${busy || !sel || !drops || !drops.drops.length} onClick=${add}>Add</button>
      </div>
    </section>`;
}

export function Planner({ league, version }) {
  const [week, setWeek] = useState('this');
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [sheet, setSheet] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let gone = false;
    setError(null);
    api.week(league, week)
      .then((got) => { if (!gone) setData(got); })
      .catch((e) => { if (!gone) { setData(null); setError(e); } });
    return () => { gone = true; };
  }, [league, version, week]);

  const edit = async (body) => {
    setBusy(true);
    try {
      setData(await api.weekEdit(league, { week, ...body }));
      setError(null);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  if (error && !data) {
    return error.status === 404
      ? html`<div class="empty">No plan saved for today yet. Tap Quick to make one.</div>`
      : error.status === 409
        ? html`<div class="empty">${error.message}</div>`
        : html`<div class="error" role="alert">${error.message}</div>`;
  }
  if (!data) return html`<div class="empty">Loading…</div>`;
  const left = data.moves_left - data.used;
  const note = data.picks
    ? `${data.picks} pick${data.picks > 1 ? 's' : ''} · ${data.used} move${data.used === 1 ? '' : 's'} used, ${left} left`
    : `No picks yet · ${data.moves_left} move${data.moves_left === 1 ? '' : 's'} left this week`;
  return html`
    <div class="sec"><h2>Weekly planner</h2>
      ${data.has_next && html`<div class="seg" role="group" aria-label="Week">
        <button type="button" aria-pressed=${week === 'this'} onClick=${() => setWeek('this')}>Week ${data.week}</button>
        <button type="button" aria-pressed=${week === 'next'} onClick=${() => setWeek('next')}>Week ${data.next_week}</button>
      </div>`}
    </div>
    <div class="week-note">
      <span>${note}${data.left_out ? html`<span class="bad"> · ${data.left_out} left out</span>` : ''}</span>
      <button type="button" class="btn small" disabled=${!data.picks || busy} onClick=${() => edit({ action: 'clear' })}>Clear this week's picks</button>
    </div>
    ${error && html`<div class="error" role="alert">${error.message}</div>`}
    ${!data.nights.length ? html`<div class="empty">No nights left to plan this week.</div>` : html`
    <div class="table-scroll">
      <table class="cal">
        <thead><tr><th scope="col" class="slot">Slot</th>
          ${data.nights.map((n) => html`<th scope="col" class="day"><span class="t">${dayTitle(n.day)}</span>
            <span class="p">${f1(n.points)} pts · <span class=${n.open ? 'bad' : ''}>${n.open} open</span></span></th>`)}
        </tr></thead>
        <tbody>
          <tr><th scope="row" class="slot picks-h">Picks</th>
            ${data.nights.map((n) => html`<td class="day picks">${n.picks.map((m) => html`
              <div class=${`pick ${m.problem ? 'out' : ''}`}>
                <span>${short(m.add_name)} ${m.drop ? `← ${short(m.drop_name)}` : '(open spot)'}${m.kind === 'claim' && !m.problem ? ` · claim, from ${weekday(m.effective)}` : ''}
                  ${m.problem && html`<br /><span class="why">left out: ${m.problem}</span>`}</span>
                <button type="button" aria-label=${`Take out ${m.add_name}`} disabled=${busy}
                  onClick=${() => edit({ action: 'unpick', day: m.picked, add: m.add })}>✕</button>
              </div>`)}</td>`)}
          </tr>
          ${data.slot_rows.map((slot, r) => html`<tr class=${r > 0 && data.slot_rows[r - 1] !== slot ? 'sep' : ''}>
            <th scope="row" class="slot">${slot}</th>
            ${data.nights.map((n) => {
              const c = n.cells[r];
              if (!c.player_id) {
                return html`<td class="day open"><button type="button" class="cellbtn" disabled=${busy}
                  aria-label=${`${dayTitle(n.day)} ${slot}: open, pick a free agent`}
                  onClick=${() => setSheet({ day: n.day, slot, occupant: null })}>
                  <span class="nm">(open) +</span><span class="pt">tap to fill</span></button></td>`;
              }
              if (c.rental) {
                return html`<td class="day rental"><button type="button" class="cellbtn" disabled=${busy}
                  aria-label=${`${dayTitle(n.day)} ${slot}: ${c.player}, your pickup, tap to replace`}
                  onClick=${() => setSheet({ day: n.day, slot, occupant: c })}>
                  <span class="nm">${short(c.player)} ⇄</span><span class="pt">${f1(c.pts)}</span></button></td>`;
              }
              return html`<td class="day"><div class="cellbtn held" title=${c.player}>
                <span class="nm">${short(c.player)}</span><span class="pt">${f1(c.pts)}</span></div></td>`;
            })}
          </tr>`)}
        </tbody>
      </table>
    </div>
    <div class="legend"><span><span style="color: #9FD0FF">■</span> your pickup ⇄</span><span><span style="color: #FF9C9C">■</span> open slot +</span><span>Tap either to choose who goes in</span></div>`}
    ${sheet && html`<${Sheet} league=${league} week=${week} sheet=${sheet}
      onClose=${() => setSheet(null)} onDone=${(got) => { setData(got); setSheet(null); }} />`}`;
}
