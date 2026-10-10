// The AI suggestions screen (plan P2), from GET /suggestions (the newest plan): today's moves, the
// upgrades, and the week plans -- a plan opens to its slots, a slot to its options, and an option
// picks him into your week with the plan's drop (POST /choices/pick; again takes him back).
import { html, useState, useEffect } from './vendor/htm-preact-3.1.1.mjs';
import { api } from './api.js';

const TAGS = {
  IR: ['#26304A', '#C9D4E6'], ADD: ['#123A2C', '#7DF0C4'], CLAIM: ['#2B240E', '#F2D88A'], DROP: ['#3B1216', '#FF8A8A'],
};
const TEAM = /\s*\(([A-Z]{2,3})\)\s*$/;

const store = {
  get(key) { try { return localStorage.getItem(key); } catch (_) { return null; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch (_) { /* private window */ } },
};

const sg = (x) => (x === null || x === undefined ? '' : (x > 0 ? '+' : x < 0 ? '−' : '') + Math.abs(x).toFixed(1));
const f1 = (x) => (x === null || x === undefined ? '' : Number(x).toFixed(1));
const f2 = (x) => (x === null || x === undefined ? '' : Number(x).toFixed(2));

// "Yaroslav Askarov (SJS)" -> {name: "Y. Askarov", team: "SJS"}
function short(full) {
  if (!full) return { name: '', team: '' };
  const m = full.match(TEAM);
  const bare = full.replace(TEAM, '');
  const i = bare.indexOf(' ');
  return { name: i > 0 ? `${bare[0]}. ${bare.slice(i + 1)}` : bare, team: m ? m[1] : '' };
}

function weekday(iso) {
  const [y, m, d] = iso.split('-').map(Number);
  return new Date(y, m - 1, d).toLocaleDateString('en-US', { weekday: 'short' });
}

function tagOf(kind) {
  const k = kind.toUpperCase();
  return k.startsWith('IR') ? TAGS.IR : k.includes('CLAIM') ? TAGS.CLAIM : k === 'DROP' ? TAGS.DROP : TAGS.ADD;
}

function Today({ data }) {
  if (!data.today.length) return html`<div class="empty">No moves today.</div>`;
  return html`
    <div class="sec"><h2>Today's moves</h2><span class="note">make them on ${data.platform}</span></div>
    <table class="list">
      <thead><tr><th scope="col" style="width: 86px">Move</th><th scope="col">Add</th><th scope="col">Drop</th></tr></thead>
      <tbody>${data.today.map((m) => {
        const [bg, fg] = tagOf(m.kind);
        const add = short(m.add), drop = short(m.drop);
        return html`<tr class="hi">
          <td><span class="tag" style=${`background: ${bg}; color: ${fg}`}>${m.kind}</span></td>
          <td>${m.add && html`<span class="good">+</span> ${add.name} <span class="meta">${add.team}${m.add_rate ? ` · ${f2(m.add_rate)}/g` : ''}</span>`}</td>
          <td class="dim2">${m.drop && html`<span class="bad">−</span> ${drop.name} <span class="meta">${drop.team}</span>`}</td>
        </tr>
        ${m.note && html`<tr class="sub-note"><td></td><td colspan="2">${m.note}</td></tr>`}`;
      })}</tbody>
    </table>`;
}

function Upgrades({ data }) {
  if (!data.upgrades.length) return html`<div class="empty">No pickups priced.</div>`;
  return html`
    <div class="sec"><h2>Upgrades</h2><span class="note">gain over the pricing window · edge = gain − bar</span></div>
    <table class="list fixed">
      <thead><tr><th scope="col">Add</th><th scope="col" style="width: 30%">Drop</th>
        <th scope="col" class="n" style="width: 50px">Gain</th><th scope="col" class="n" style="width: 42px">Bar</th>
        <th scope="col" class="n" style="width: 56px">Edge</th></tr></thead>
      <tbody>${data.upgrades.map((u) => {
        const add = short(u.add), drop = short(u.drop);
        return html`<tr class=${u.clears ? 'hi' : ''}>
          <td><b>${add.name}</b>${u.in_plan && html` <span class="tag" style="background: #123A2C; color: #7DF0C4">plan</span>`}
            <br /><span class="meta">${add.team} · ${f2(u.add_rate)}/g · ${u.add_games} gp</span></td>
          <td class="dim2">${u.drop ? drop.name : '(open spot)'}</td>
          <td class="n">${sg(u.gain)}</td><td class="n dim">${f1(u.bar)}</td>
          <td class=${`n ${u.clears ? 'good' : 'dim'}`}>${sg(u.edge)}</td>
        </tr>`;
      })}</tbody>
    </table>
    <p class="foot">${data.clearing
      ? 'Green clears the bar. Week plans are planned after today\'s upgrades.'
      : 'None clears the bar today, so the plan makes no upgrade. Week plans are planned after today\'s upgrades.'}</p>`;
}

function Plans({ data, week, onWeek, onPick, busy }) {
  const [open, setOpen] = useState(() => new Set());
  const flip = (key) => setOpen((s) => { const n = new Set(s); n.has(key) ? n.delete(key) : n.add(key); return n; });
  const w = data.week;
  return html`
    <div class="sec"><h2>Week plans</h2>
      ${w.has_next && html`<div class="seg" role="group" aria-label="Week">
        <button type="button" aria-pressed=${week === 'this'} onClick=${() => onWeek('this')}>Week ${w.week}</button>
        <button type="button" aria-pressed=${week === 'next'} onClick=${() => onWeek('next')}>Week ${w.next_week}</button>
      </div>`}
    </div>
    ${!w.plans.length ? html`<div class="empty">${w.empty}</div>` : html`
      <div class="hint-line">Tap a plan for its slots, a slot for its options, an option to pick him into your Weekly planner.</div>
      ${w.plans.map((pl) => {
        const key = `${w.which}:${pl.label}`;
        const isOpen = open.has(key);
        const meta = [pl.games ? `${pl.games} games` : '', pl.slots.length === 1 ? '1 move' : `${pl.slots.length} moves`,
          pl.without.length ? `without ${pl.without.join(', ')}` : ''].filter(Boolean).join(' · ');
        return html`
          <button type="button" class=${`plan ${isOpen ? 'open' : ''}`} aria-expanded=${isOpen} onClick=${() => flip(key)}>
            <span class="label">${pl.label}</span>
            <span class="what"><span class="sched">${pl.schedule.join(' → ')}</span><span class="meta">${meta}</span></span>
            <span class="nums"><span class="good">${sg(pl.week_gain)}</span><span class="meta">edge ${sg(pl.week_edge)}</span></span>
          </button>
          ${isOpen && html`<table class="list fixed slots">
            <thead><tr><th scope="col" style="width: 84px">Day · slot</th><th scope="col">Best → drop</th>
              <th scope="col" class="n" style="width: 50px">Gain</th><th scope="col" class="n" style="width: 42px">Bar</th>
              <th scope="col" class="n" style="width: 56px">Edge</th></tr></thead>
            <tbody>${pl.slots.map((s) => {
              const skey = `${key}:${s.index}`;
              const sOpen = open.has(skey);
              const from = s.from !== s.day ? ` (from ${weekday(s.from)})` : '';
              return html`
                <tr class=${s.picked ? 'mine' : ''}>
                  <td>${weekday(s.day)} ${s.team || ''} ${s.pos || ''}</td>
                  <td>${s.options.length || s.near.length
                    ? html`<button type="button" class="link" aria-expanded=${sOpen} onClick=${() => flip(skey)}>
                        ${sOpen ? '▾' : '▸'} <b>${short(s.add).name}</b>${from} <span class="meta">→ ${s.drop ? short(s.drop).name : 'open spot'}</span></button>`
                    : html`<b>${short(s.add).name}</b>${from} <span class="meta">→ ${s.drop ? short(s.drop).name : 'open spot'}</span>`}</td>
                  <td class="n">${sg(s.gain)}</td><td class="n dim">${f1(s.bar)}</td><td class="n">${sg(s.edge)}</td>
                </tr>
                ${sOpen && s.options.map((o) => html`
                  <tr class=${o.picked ? 'mine' : 'opt'}>
                    <td class="state">${o.picked ? 'picked' : ''}</td>
                    <td><button type="button" class="link" aria-pressed=${o.picked} disabled=${busy}
                      onClick=${() => onPick(pl.label, s.index, o.player_id)}
                      aria-label=${`${o.picked ? 'Take back' : 'Pick'} ${o.add}`}>
                      ${o.rank}. ${short(o.add).name}${o.status && o.status !== 'ACTIVE' ? ` [${o.status}]` : ''}${' '}<span class="meta">${o.positions}${o.from !== s.from ? ` · from ${weekday(o.from)}` : ''}</span></button></td>
                    <td class="n dim2">${sg(o.gain)}</td><td class="n dim">${f1(o.bar)}</td><td class="n dim2">${sg(o.edge)}</td>
                  </tr>`)}
                ${sOpen && s.near.map((n) => html`
                  <tr class="opt"><td class="state dim">other team</td>
                    <td class="dim">≈ ${short(n.add).name} <span class="meta">${n.team} · ${n.positions}</span></td>
                    <td colspan="3" class="n dim">${sg(n.delta)} vs pick</td></tr>`)}`;
            })}</tbody>
          </table>`}`;
      })}
      <p class="foot">Plan A is the model's best week; its moves today are in Today's moves. B, C, … each leave out every earlier plan's first team.</p>`}`;
}

export function Suggestions({ league, version }) {
  const [tab, setTab] = useState(() => store.get('fh.ai.tab') || 'today');
  const [week, setWeek] = useState('this');
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let gone = false;
    setError(null);
    api.suggestions(league, week)
      .then((got) => { if (!gone) setData(got); })
      .catch((e) => { if (!gone) { setData(null); setError(e); } });
    return () => { gone = true; };
  }, [league, version, week]);

  const choose = (t) => { setTab(t); store.set('fh.ai.tab', t); };
  const pick = async (label, index, playerId) => {
    setBusy(true);
    try {
      setData(await api.pick(league, { week, label, index, player_id: playerId }));
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
      : html`<div class="error" role="alert">${error.message}</div>`;
  }
  if (!data) return html`<div class="empty">Loading…</div>`;
  const tabs = [['today', 'Today', data.today.length], ['upgrades', 'Upgrades', data.clearing],
    ['plans', 'Week plans', data.week.plans.length]];
  return html`
    <nav class="subtabs" aria-label="AI suggestions">
      ${tabs.map(([id, label, count]) => html`<button type="button" aria-pressed=${tab === id} onClick=${() => choose(id)}>
        ${label}<span class=${`badge ${count ? '' : 'zero'}`}>${count}</span></button>`)}
    </nav>
    ${error && html`<div class="error" role="alert">${error.message}</div>`}
    ${tab === 'today' && html`<${Today} data=${data} />`}
    ${tab === 'upgrades' && html`<${Upgrades} data=${data} />`}
    ${tab === 'plans' && html`<${Plans} data=${data} week=${week} onWeek=${setWeek} onPick=${pick} busy=${busy} />`}`;
}
