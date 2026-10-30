// The Plan tab (redesign plan app-redesign.md, step 3; was the Weekly planner, P3, and the AI
// suggestions, P2): today's moves and the upgrades in a banner at the top (GET /suggestions), then
// your week built from your picks (GET /week: the server puts them in on plan A's workbench and
// solves each night's lineup). The chips above the calendar draw one of the AI's week plans over
// it instead (GET /week?show=A): its picks and pickups that are not yours yet dashed, each one
// taken with a tap (POST /week/edit 'take') or the whole plan with Use (POST /week/edit 'use').
// Tap an open slot (+), one of your pickups (⇄) or a rostered player for the picker: the free
// agents who would start that night, the plans' picks first (GET /week/picker), and who each may
// drop (GET /week/drops). Every change goes through POST /week/edit and the calendar comes back as
// it now stands. The bench -- held but not starting -- is one row of counts, its players shown
// beneath it only when opened. On a PC (`wide`) the picker is a panel beside the calendar.
import { html, useState, useEffect } from './vendor/htm-preact-3.1.1.mjs';
import { api } from './api.js';
import { useEscape, NoPlan, askRefresh } from './layout.js';

const TEAM = /\s*\(([A-Z]{2,3})\)\s*$/;
const TAGS = {
  IR: ['#26304A', '#C9D4E6'], ADD: ['#123A2C', '#7DF0C4'], CLAIM: ['#2B240E', '#F2D88A'], DROP: ['#3B1216', '#FF8A8A'],
};
const UPGRADES_SHOWN = 5;     // the banner's upgrades, best edge first, before "+N more"
const f2 = (x) => (x === null || x === undefined ? '' : Number(x).toFixed(2));
const sg = (x) => (x === null || x === undefined ? '' : (x > 0 ? '+' : x < 0 ? '−' : '') + Math.abs(x).toFixed(2));

const store = {
  get(key) { try { return localStorage.getItem(key); } catch (_) { return null; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch (_) { /* private window */ } },
};

function short(full) {
  if (!full) return '';
  const bare = full.replace(TEAM, '');
  const i = bare.indexOf(' ');
  return i > 0 ? `${bare[0]}. ${bare.slice(i + 1)}` : bare;
}
const teamOf = (full) => ((full || '').match(TEAM) || [])[1] || '';
const last = (full) => short(full).split(' ').pop();

function dayTitle(iso) {
  const [y, m, d] = iso.slice(0, 10).split('-').map(Number);
  const t = new Date(y, m - 1, d);
  return `${t.toLocaleDateString('en-US', { weekday: 'short' })} ${t.toLocaleDateString('en-US', { month: 'short' })} ${d}`;
}
const weekday = (iso) => dayTitle(iso).split(' ')[0];

function tagOf(kind) {
  const k = kind.toUpperCase();
  return k.startsWith('IR') ? TAGS.IR : k.includes('CLAIM') ? TAGS.CLAIM : k === 'DROP' ? TAGS.DROP : TAGS.ADD;
}

// Today's moves and the upgrades (were the AI tab's Today and Upgrades): a count and a line,
// opened for the moves, the upgrades' edges and whether the week plans are made after today's
// upgrades (Together) or apart from them (the user, 2026-10-20).
function TodayBanner({ sugg, onTogether, busy }) {
  const [open, setOpen] = useState(false);
  const [allUps, setAllUps] = useState(false);     // the upgrades past the first UPGRADES_SHOWN
  const today = sugg.today;
  const ups = allUps ? sugg.upgrades : sugg.upgrades.slice(0, UPGRADES_SHOWN);
  const one = (m) => (m.add && m.drop ? `${m.kind.split(':')[0]} ${last(m.add)} ← ${last(m.drop)}`
    : m.add ? `${m.kind} ${last(m.add)}` : `${m.kind} ${last(m.drop)}`);
  const summary = [...today.map(one),
    sugg.clearing ? `${sugg.clearing} upgrade${sugg.clearing === 1 ? '' : 's'} clear${sugg.clearing === 1 ? 's' : ''} the bar` : 'no upgrade clears the bar']
    .join(' · ');
  const chosen = sugg.together_chosen;
  return html`<section class="today-banner" aria-label="Today's moves">
    <button type="button" class="tb-head" aria-expanded=${open} onClick=${() => setOpen(!open)}>
      <span class=${`tb-count ${today.length ? '' : 'zero'}`} aria-hidden="true">${today.length}</span>
      <span class="tb-what">
        <span class="tb-title">${today.length ? 'Moves to make today' : 'No moves today'} <span class="meta">${dayTitle(sugg.day)} · on ${sugg.platform}</span></span>
        <span class="tb-sum">${summary}</span>
      </span>
      <svg aria-hidden="true" class=${open ? 'flip' : ''} width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"></path></svg>
    </button>
    ${open && html`<div class="tb-body">
      ${today.map((m) => {
        const [bg, fg] = tagOf(m.kind);
        return html`<div class="tb-move">
          <span class="tag" style=${`background: ${bg}; color: ${fg}`}>${m.kind}</span>
          <span>${m.add && html`<span class="good">+</span> ${short(m.add)}`}${m.add && m.drop && html` <span class="dim">for</span> `}${m.drop && (m.add ? short(m.drop) : html`<span class="bad">−</span> ${short(m.drop)}`)}</span>
          ${m.note && html`<span></span><span class="meta">${m.note}</span>`}
        </div>`;
      })}
      <div class="tb-sec">Upgrades · gain over the bar</div>
      ${!sugg.upgrades.length && html`<div class="meta">No pickups priced.</div>`}
      ${ups.map((u) => html`<div class=${`tb-up ${u.clears ? '' : 'dim'}`}>
        <span class="who">${short(u.add)} <span class="dim">for</span> ${u.drop ? short(u.drop) : 'open spot'}${u.in_plan ? html` <span class="tag" style="background: #123A2C; color: #7DF0C4">plan</span>` : ''}</span>
        <span class=${`edge mono ${u.clears ? 'good' : 'dim'}`} title=${`gain ${sg(u.gain)} − bar ${f2(u.bar)}`}>edge ${sg(u.edge)}</span>
      </div>`)}
      ${sugg.upgrades.length > UPGRADES_SHOWN && html`<button type="button" class="link-btn you tb-more" onClick=${() => setAllUps(!allUps)}>
        ${allUps ? 'Fewer' : `+${sugg.upgrades.length - UPGRADES_SHOWN} more`}</button>`}
      <div class="tb-mode">
        <span class="meta">${chosen !== sugg.together ? 'Re-planning…'
          : chosen ? "Week plans are made after today's upgrades" : 'Week plans are made apart from them'}</span>
        <div class="seg" role="group" aria-label="Week plans and upgrades">
          <button type="button" aria-pressed=${chosen} disabled=${busy} onClick=${() => onTogether(true)}>Together</button>
          <button type="button" aria-pressed=${!chosen} disabled=${busy} onClick=${() => onTogether(false)}>Apart</button>
        </div>
      </div>
    </div>`}
  </section>`;
}

function Sheet({ league, week, sheet, onClose, onDone, docked }) {
  const [list, setList] = useState(null);
  const [error, setError] = useState(null);
  const [filter, setFilter] = useState('All');
  const [sel, setSel] = useState(null);
  const [drops, setDrops] = useState(null);
  const [drop, setDrop] = useState(undefined);
  const [busy, setBusy] = useState(false);
  const occupant = sheet.occupant;

  useEffect(() => {
    api.picker(league, week, sheet.day, occupant && occupant.player_id, sheet.slot)
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
      await onDone(occupant
        ? { action: 'replace', day: sheet.day, add: sel.player_id, rental: occupant.player_id }
        : { action: 'pick', day: sheet.day, add: sel.player_id, drop: drop === undefined ? null : drop });
    } catch (e) {
      setError(e.message);
      setBusy(false);
    }
  };

  useEscape(true, onClose);
  const shown = list ? list.adds.filter((f) => filter === 'All' || f.positions.split('/').includes(filter)) : [];
  const counts = list && list.counts_from !== sheet.day ? ` (adds count from ${weekday(list.counts_from)})` : '';
  return html`
    ${!docked && html`<div class="scrim" onClick=${onClose}></div>`}
    <section class=${docked ? 'dock' : 'sheet'} role=${docked ? 'region' : 'dialog'} aria-label=${`Add on ${dayTitle(sheet.day)}`}>
      <div class="sheet-head">
        <span class="title">${dayTitle(sheet.day)} · ${sheet.slot} slot</span>
        <span class="sub">${occupant
          ? (occupant.rental ? `Replacing ${short(occupant.player)} from this night`
            : html`Swapping out ${short(occupant.player)} from this night: <span class="bad">he is dropped for good</span>, and the week gain does not count what he is worth after this week`)
          : 'Open'}${counts}.
          ${occupant
            ? `Free agents who can play ${sheet.slot === 'BN' ? 'any position' : `the ${sheet.slot} slot`} that night: the plans' picks first, then the most points that night. Week: the week's lineup points with the swap.`
            : `Free agents who can play the ${sheet.slot} slot and would start that night: the plans' picks first, then the most points that night. Week: the lineup points he adds this week.`}</span>
      </div>
      ${error && html`<div class="error" role="alert">${error}</div>`}
      ${list && list.positions.length > 1 && html`
        <div class="filters"><span>Position</span>
          <div class="seg" role="group" aria-label="Position filter">
            ${['All', ...list.positions].map((q) => html`<button type="button" aria-pressed=${filter === q} onClick=${() => setFilter(q)}>${q}</button>`)}
          </div></div>`}
      <div class="adds">
        ${!list ? html`<div class="empty">Loading…</div>` : !shown.length
          ? html`<p class="foot">${list.dropped_by
            ? `${short(list.occupant_name)} is already dropped on ${dayTitle(list.dropped_by.day)} for ${short(list.dropped_by.add_name)}: take that pick out to swap him out here.`
            : list.moves_full ? "Your picks use up the week's moves left: take one out to add here."
            : 'No free agent who plays that night would start.'}</p>`
          : html`<table class="list fixed">
            <thead><tr><th scope="col" style="width: 54px">AI</th><th scope="col">Player</th>
              <th scope="col" class="n" style="width: 50px">Pts</th><th scope="col" class="n" style="width: 58px">Week</th>
              <th scope="col" class="n" style="width: 48px">Left</th></tr></thead>
            <tbody>${shown.map((f) => html`
              <tr class=${sel && sel.player_id === f.player_id ? 'sel' : f.plans ? 'mine' : ''}>
                <td class="state">${f.plans}</td>
                <td><button type="button" class="link" aria-pressed=${!!sel && sel.player_id === f.player_id}
                  onClick=${() => choosePlayer(f)}>${short(f.player)}${f.status && f.status !== 'ACTIVE' ? ` [${f.status}]` : ''}${' '}<span class="meta">${f.positions} · ${teamOf(f.player)}</span>${f.days && html`<span class="days">${f.days.map(weekday).join(' ')}</span>`}</button></td>
                <td class="n">${f2(f.pts)}</td><td class="n good">${sg(f.gain)}</td><td class="n dim">${f.games}</td>
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

export function Plan({ league, version, wide }) {
  const [week, setWeek] = useState('this');
  // The chip shown: a week plan's label, or 'mine' for your picks alone; A until changed, kept here.
  const [show, setShow] = useState(() => store.get('fh.plan.show') || 'A');
  const [data, setData] = useState(null);
  const [sugg, setSugg] = useState(null);
  const [error, setError] = useState(null);
  const [sheet, setSheet] = useState(null);
  const [busy, setBusy] = useState(false);
  const [benchOpen, setBenchOpen] = useState(false);     // the bench rows: closed until opened
  const asked = show === 'mine' ? undefined : show;

  useEffect(() => {
    let gone = false;
    setError(null);
    api.week(league, week, asked)
      .then((got) => { if (!gone) setData(got); })
      .catch((e) => { if (!gone) { setData(null); setError(e); } });
    return () => { gone = true; };
  }, [league, version, week, show]);

  useEffect(() => {
    let gone = false;
    api.suggestions(league, week)
      .then((got) => { if (!gone) setSugg(got); })
      .catch(() => { if (!gone) setSugg(null); });     // the banner waits; the calendar stands alone
    return () => { gone = true; };
  }, [league, version, week]);

  const choose = (label) => { setShow(label); store.set('fh.plan.show', label); setSheet(null); };
  const edit = async (body) => {
    setBusy(true);
    try {
      setData(await api.weekEdit(league, { week, show: asked, ...body }));
      setError(null);
    } catch (e) {
      setError(e);
      throw e;
    } finally {
      setBusy(false);
    }
  };
  const tryEdit = (body) => edit(body).catch(() => { /* shown in the error line */ });
  // Saved, then re-planned (a 'plan' on the kept read, in App's refresh strip): the screen
  // re-fetches when it ends.
  const setTogether = async (on) => {
    if (!sugg || on === sugg.together_chosen) return;
    setBusy(true);
    try {
      await api.together(league, { on, week });
      setSugg({ ...sugg, together_chosen: on });
      askRefresh('plan');
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  const banner = sugg && html`<${TodayBanner} sugg=${sugg} onTogether=${setTogether} busy=${busy} />`;
  if (error && !data) {
    return error.status === 404
      ? html`<${NoPlan} />`
      : error.status === 409
        ? html`${banner}<div class="empty">${error.message}</div>`
        : html`<div class="error" role="alert">${error.message}</div>`;
  }
  if (!data) return html`<div class="empty">Loading…</div>`;
  const shown = data.shown;          // the plan drawn, or null: your picks
  const left = data.moves_left - data.used;
  const note = data.picks
    ? `${data.picks} pick${data.picks > 1 ? 's' : ''} · ${left} of ${data.moves_left} move${data.moves_left === 1 ? '' : 's'} left`
    : `No picks yet · ${data.moves_left} move${data.moves_left === 1 ? '' : 's'} left`;
  // The shown plan's picks that are not yours: by the player, its slot in the plan (to take him).
  const ghostIndex = {};
  for (const n of data.nights) for (const m of n.picks) if (m.ghost && m.index !== null) ghostIndex[m.add] = m.index;
  const ghosts = Object.keys(ghostIndex).length;
  const sheetKey = sheet && `${sheet.day}:${sheet.slot}:${sheet.occupant ? sheet.occupant.player_id : ''}`;
  // A held player's cell -- the shown plan's pickup not yours (dashed, tap to take), your pickup
  // (⇄, to replace) or a rostered player (to swap out) -- in slot row `row` (a lineup row's
  // index, or `b<i>` for the bench's).
  const heldCell = (n, slot, row, c, pts, extra = '') => {
    const chosen = sheet && sheet.day === n.day && sheet.row === row ? ' chosen' : '';
    if (c.ghost && ghostIndex[c.player_id] !== undefined) {
      return html`<td class=${`day ghost${extra}${chosen}`}><button type="button" class="cellbtn" disabled=${busy} title=${c.player}
        aria-label=${`${dayTitle(n.day)} ${slot}: ${c.player}, plan ${shown}'s pickup, tap to add him to your picks`}
        onClick=${() => tryEdit({ action: 'take', label: shown, index: ghostIndex[c.player_id] })}>
        <span class="nm">+ ${short(c.player)}</span><span class="pt">${pts} · plan ${shown}</span></button></td>`;
    }
    const what = c.rental ? 'your pickup, tap to replace' : 'rostered, tap to swap for a rental';
    return html`<td class=${`day${c.rental ? ' rental' : ''}${extra}${chosen}`}><button type="button"
      class=${`cellbtn${c.rental ? '' : ' held'}`} disabled=${busy} title=${c.player}
      aria-label=${`${dayTitle(n.day)} ${slot}: ${c.player}, ${what}`}
      onClick=${() => setSheet({ day: n.day, slot, row, occupant: c })}>
      <span class="nm">${short(c.player)}${c.rental ? ' ⇄' : ''}</span><span class="pt">${pts}</span></button></td>`;
  };
  const benchRows = Math.max(0, ...data.nights.map((n) => n.bench.length));
  const picker = sheet && html`<${Sheet} key=${sheetKey} league=${league} week=${week} sheet=${sheet} docked=${wide}
      onClose=${() => setSheet(null)} onDone=${async (body) => { await edit(body); setSheet(null); }} />`;
  const chips = data.plans.length ? [['mine', 'Mine', `${f2(data.mine_points)} pts`, false],
    ...data.plans.map((p) => [p.label, `Plan ${p.label}`, `${sg(p.points - data.mine_points)} vs mine`, p.points > data.mine_points + 0.005])] : [];
  const calendar = html`
    <div class="plan-top">
      ${data.has_next ? html`<div class="seg" role="group" aria-label="Week">
        <button type="button" aria-pressed=${week === 'this'} onClick=${() => setWeek('this')}>Week ${data.week}</button>
        <button type="button" aria-pressed=${week === 'next'} onClick=${() => setWeek('next')}>Week ${data.next_week}</button>
      </div>` : html`<span class="wk">Week ${data.which === 'next' ? data.next_week : data.week}</span>`}
      <span class="note">${note}${data.left_out ? html`<span class="bad"> · ${data.left_out} left out</span>` : ''}</span>
    </div>
    ${chips.length > 0 && html`<div class="plan-chips">
      <div class="chips-row" role="group" aria-label="Show plan">
        ${chips.map(([key, label, sub, better]) => {
          const on = key === 'mine' ? !shown : shown === key;
          return html`<button type="button" class=${`pchip ${key === 'mine' ? 'mine' : ''}`} aria-pressed=${on} onClick=${() => choose(key)}>
            <span class="l">${label}</span><span class=${`s ${better && !on ? 'good' : ''}`}>${sub}</span></button>`;
        })}
      </div>
      ${shown && ghosts > 0 && html`<button type="button" class="btn primary use" disabled=${busy}
        onClick=${() => tryEdit({ action: 'use', label: shown })}>Use ${shown}</button>`}
      ${!shown && data.picks > 0 && html`<button type="button" class="btn use" disabled=${busy}
        onClick=${() => tryEdit({ action: 'clear' })}>Clear picks</button>`}
    </div>`}
    ${error && html`<div class="error" role="alert">${error.message}</div>`}
    ${!data.nights.length ? html`<div class="empty">No nights left to plan this week.</div>` : html`
    <div class="table-scroll">
      <table class="cal">
        <thead><tr><th scope="col" class="slot">Slot</th>
          ${data.nights.map((n) => html`<th scope="col" class="day"><span class="t">${dayTitle(n.day)}</span>
            <span class="p">${f2(n.points)} pts · <span class=${n.open ? 'bad' : ''}>${n.open} open</span></span></th>`)}
        </tr></thead>
        <tbody>
          <tr><th scope="row" class="slot picks-h">Picks</th>
            ${data.nights.map((n) => html`<td class="day picks">${n.picks.map((m) => html`
              <div class=${`pick ${m.problem ? 'out' : ''} ${m.ghost ? 'ghost' : ''}`}>
                <span>${m.ghost ? `${shown}: ` : ''}${short(m.add_name)} ${m.drop ? `← ${short(m.drop_name)}` : '(open spot)'}${m.kind === 'claim' && !m.problem ? ` · claim, from ${weekday(m.effective)}` : ''}
                  ${m.problem && html`<br /><span class="why">left out: ${m.problem}</span>`}</span>
                ${m.ghost
                  ? html`<button type="button" class="take" aria-label=${`Add ${m.add_name} to your picks`} disabled=${busy || m.index === null}
                      onClick=${() => tryEdit({ action: 'take', label: shown, index: m.index })}>+</button>`
                  : html`<button type="button" aria-label=${`Take out ${m.add_name}`} disabled=${busy}
                      onClick=${() => tryEdit({ action: 'unpick', day: m.picked, add: m.add })}>✕</button>`}
              </div>`)}</td>`)}
          </tr>
          ${data.slot_rows.map((slot, r) => html`<tr class=${r > 0 && data.slot_rows[r - 1] !== slot ? 'sep' : ''}>
            <th scope="row" class="slot">${slot}</th>
            ${data.nights.map((n) => {
              const c = n.cells[r];
              const chosen = sheet && sheet.day === n.day && sheet.row === r ? ' chosen' : '';
              if (!c.player_id) {
                return html`<td class=${`day open${chosen}`}><button type="button" class="cellbtn" disabled=${busy}
                  aria-label=${`${dayTitle(n.day)} ${slot}: open, pick a free agent`}
                  onClick=${() => setSheet({ day: n.day, slot, row: r, occupant: null })}>
                  <span class="nm">(open) +</span><span class="pt">tap to fill</span></button></td>`;
              }
              return heldCell(n, slot, r, c, f2(c.pts));
            })}
          </tr>`)}
          ${benchRows > 0 && html`<tr class="sep bench-h">
            <th scope="row" class="slot"><button type="button" class="bench-toggle" aria-expanded=${benchOpen}
              aria-label=${benchOpen ? 'Hide the bench' : 'Show the bench'} onClick=${() => setBenchOpen(!benchOpen)}>${benchOpen ? '▾' : '▸'} BN</button></th>
            ${data.nights.map((n) => {
              const plays = n.bench.filter((c) => c.plays).length;
              return html`<td class="day"><button type="button" class="cellbtn bench-sum" aria-expanded=${benchOpen}
                onClick=${() => setBenchOpen(!benchOpen)}><span class="nm">${n.bench.length} on the bench</span>
                <span class="pt">${plays} play${plays === 1 ? 's' : ''} that night</span></button></td>`;
            })}
          </tr>`}
          ${benchRows > 0 && benchOpen && Array.from({ length: benchRows }, (_, i) => html`<tr class="bench">
            <th scope="row" class="slot">BN</th>
            ${data.nights.map((n) => {
              const c = n.bench[i];
              if (!c) return html`<td class="day"></td>`;
              const pts = c.plays ? f2(c.pts) : (c.status && c.status !== 'ACTIVE' ? c.status : 'no game');
              return heldCell(n, 'BN', `b${i}`, c, pts, c.plays ? '' : ' idle');
            })}
          </tr>`)}
        </tbody>
      </table>
    </div>
    <div class="legend"><span><span style="color: #9FD0FF">■</span> your pickup ⇄</span>
      <span><span class="ghost-key"></span> ${shown ? `Plan ${shown}` : 'A plan'}'s pickup, ${wide ? 'click' : 'tap'} to add</span>
      <span><span style="color: #FF9C9C">■</span> open slot +</span>
      <span>${wide ? 'Click' : 'Tap'} a cell to choose who goes in; the picker lists the AI's picks first</span></div>`}`;
  if (!wide) return html`${banner}${calendar}${picker}`;
  return html`<div class="split">
    <div class="split-main">${banner}${calendar}</div>
    ${picker || html`<aside class="dock idle"><p class="foot">Click an open slot (+), one of your pickups (⇄) or a rostered player to choose who goes in that night.</p></aside>`}
  </div>`;
}
