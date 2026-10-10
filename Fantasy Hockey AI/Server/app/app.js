// The phone app's shell (plan P0): header with the league, the day, Quick / Full and how fresh each
// step is; the running refresh's progress; the bottom tabs; the More tab. Roster is roster.js (P1),
// AI is ai.js (P2), Planner is planner.js (P3), Games and a game's screens are games.js (P4);
// Matchup comes in P5. Everything shown comes from the plan server (api.js);
// nothing is computed here.
import { html, render, useState, useEffect, useRef, useCallback, useErrorBoundary } from './vendor/htm-preact-3.1.1.mjs';
import { api } from './api.js';
import { Roster } from './roster.js';
import { Suggestions } from './ai.js';
import { Planner } from './planner.js';
import { Games, Game } from './games.js';

const TABS = [
  ['roster', 'Roster'], ['planner', 'Planner'], ['ai', 'AI'],
  ['games', 'Games'], ['matchup', 'Matchup'], ['more', 'More'],
];
const COMING = { matchup: 'P5' };
// /status `last` steps, in the header's order, with their short names.
const STEPS = [['injuries', 'Inj'], ['goalies', 'Gl'], ['projections', 'Proj'], ['league read', 'Lg'], ['lines', 'Lines']];
const STALE_MIN = 120;          // a step older than this shows amber
const STATUS_EVERY_MS = 60000;  // follows the server's auto refreshes, as plan_gui does
const JOB_EVERY_MS = 1000;

const store = {
  get(key) { try { return localStorage.getItem(key); } catch (_) { return null; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch (_) { /* private window */ } },
};

// "2026-10-10" -> "Sat Oct 10" (a date, not a moment: no time zone shift).
function dayLabel(iso) {
  if (!iso) return '';
  const [y, m, d] = iso.split('-').map(Number);
  const date = new Date(y, m - 1, d);
  const part = (opts) => date.toLocaleDateString('en-US', opts);
  return `${part({ weekday: 'short' })} ${part({ month: 'short' })} ${d}`;
}

// The server's local times ("2026-10-10T13:55:02", no zone) are the same zone as the phone's.
function minutesAgo(iso, now) {
  return iso ? Math.max(0, Math.round((now - new Date(iso)) / 60000)) : null;
}

function age(min) {
  if (min === null) return '—';
  if (min < 1) return 'now';
  if (min < 60) return `${min}m`;
  if (min < 48 * 60) return `${Math.round(min / 60)}h`;
  return `${Math.round(min / 1440)}d`;
}

function clock(iso) {
  return iso ? new Date(iso).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' }) : '—';
}

// The route: a tab (#/roster ...), or one game (#/game/<id>/<view>), which opens full screen.
function useRoute() {
  const read = () => {
    const path = location.hash.replace(/^#\/?/, '');
    const game = path.match(/^game\/(\d+)(?:\/(summary|box|plays|lines))?$/);
    if (game) return { tab: 'games', game: Number(game[1]), view: game[2] || 'summary' };
    return { tab: TABS.some(([id]) => id === path) ? path : 'roster' };
  };
  const [route, setRoute] = useState(read);
  useEffect(() => {
    const on = () => setRoute(read());
    addEventListener('hashchange', on);
    return () => removeEventListener('hashchange', on);
  }, []);
  return route;
}

function Freshness({ status, now }) {
  if (!status) return html`<div class="fresh">…</div>`;
  const last = status.last || {};
  const shown = STEPS.filter(([key]) => key in last);
  if (!shown.length) return html`<div class="fresh">No refresh on this server today</div>`;
  return html`<div class="fresh">${shown.map(([key, label], i) => {
    const min = minutesAgo(last[key], now);
    return html`${i ? ' · ' : ''}<span class=${min > STALE_MIN ? 'warn' : ''}>${label} ${age(min)}</span>`;
  })}</div>`;
}

function Header({ status, league, onLeague, job, onRefresh, now }) {
  const [open, setOpen] = useState(false);
  const teams = (status && status.teams) || {};
  const leagues = (status && status.leagues) || [];
  const team = (teams[league] && teams[league].team) || league || 'Fantasy Hockey';
  const running = job && job.state === 'running';
  return html`
    <header class="top">
      <div class="top-row">
        <button type="button" class="league-btn" aria-haspopup="true" aria-expanded=${open}
          onClick=${() => setOpen(!open)} disabled=${leagues.length < 2}>
          <span class="team">${team}</span>
          ${leagues.length > 1 && html`<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6"></path></svg>`}
          <span class="day">${status ? dayLabel(status.day) : ''}</span>
        </button>
        <div class="refresh">
          <button type="button" class="btn primary" disabled=${running} onClick=${() => onRefresh('quick')}>Quick</button>
          <button type="button" class="btn" disabled=${running} onClick=${() => onRefresh('full')}>Full</button>
        </div>
      </div>
      <${Freshness} status=${status} now=${now} />
      ${open && html`
        <div class="menu" role="menu" aria-label="League">
          ${leagues.map((name) => html`
            <button type="button" role="menuitemradio" aria-pressed=${name === league} aria-checked=${name === league}
              onClick=${() => { setOpen(false); onLeague(name); }}>
              ${(teams[name] && teams[name].team) || name}
              <span>${name} · ${(teams[name] && teams[name].platform) || ''}</span>
            </button>`)}
        </div>`}
    </header>`;
}

function JobStrip({ job, onClose }) {
  if (!job) return null;
  const last = job.lines.length ? job.lines[job.lines.length - 1].replace(/^\d\d:\d\d:\d\d\s+/, '') : 'starting…';
  if (job.state === 'running') {
    return html`<div class="job" role="status"><span class="spin" aria-hidden="true"></span>
      <span>${job.mode === 'full' ? 'Full' : job.mode === 'plan' ? 'Re-plan' : 'Quick'}</span>
      <span class="line">${last}</span></div>`;
  }
  const failed = job.state === 'failed';
  return html`<div class="job" role="status">
    <span class=${failed ? 'state-failed' : 'state-done'}>${failed ? 'Refresh failed' : 'Refresh done'}</span>
    <span class="line">${job.error || ''}</span>
    <button type="button" onClick=${onClose} aria-label="Close">Close</button></div>`;
}

// A screen that throws shows what went wrong and a way out, instead of leaving the app frozen
// (Preact keeps the last good page and stops updating). Keyed by the route, so moving on resets it.
function Guard({ children }) {
  const [error, reset] = useErrorBoundary();
  if (error) {
    return html`<div class="error" role="alert">This screen hit an error: ${String(error.message || error)}.
      <br /><button type="button" class="btn small" style="margin-top: 8px" onClick=${() => { reset(); location.reload(); }}>Reload</button>
      ${' '}<a href="#/roster">Back to Roster</a></div>`;
  }
  return children;
}

function Tabs({ tab }) {
  return html`<nav class="tabs" aria-label="Sheets">
    ${TABS.map(([id, label]) => html`<a href=${`#/${id}`} aria-current=${id === tab ? 'page' : undefined}>${label}</a>`)}
  </nav>`;
}

function Coming({ tab }) {
  const label = TABS.find(([id]) => id === tab)[1];
  return html`<div class="empty">The ${label} screen is built in ${COMING[tab]} of the plan.</div>`;
}

function RefreshLog({ version }) {
  const [jobs, setJobs] = useState(null);
  const [error, setError] = useState(null);
  const [open, setOpen] = useState(null);
  const [lines, setLines] = useState({});
  useEffect(() => {
    api.jobs().then((got) => { setJobs(got); setError(null); }).catch((e) => setError(e.message));
  }, [version]);
  const toggle = (job) => {
    if (open === job.id) { setOpen(null); return; }
    setOpen(job.id);
    api.job(job.id, 0).then((got) => setLines((all) => ({ ...all, [job.id]: got.lines }))).catch((e) => setError(e.message));
  };
  if (error) return html`<div class="error">${error}</div>`;
  if (!jobs) return html`<div class="empty">Loading…</div>`;
  if (!jobs.length) return html`<div class="empty">No refresh since the server started.</div>`;
  return html`<div>${jobs.map((job) => {
    const secs = job.finished ? Math.round((new Date(job.finished) - new Date(job.started)) / 1000) : null;
    return html`
      <button type="button" class="log-row" aria-expanded=${open === job.id} onClick=${() => toggle(job)}>
        <span class="what">${job.mode[0].toUpperCase() + job.mode.slice(1)} · <span class=${`state-${job.state}`}>${job.state}</span></span>
        <span class="when">${clock(job.started)}${secs !== null ? ` · ${secs} s` : ''}</span>
        <span class="by">${job.by}</span>
        ${job.error && html`<span class="err">${job.error}</span>`}
      </button>
      ${open === job.id && html`<pre class="lines">${(lines[job.id] || ['…']).join('\n')}</pre>`}`;
  })}</div>`;
}

function More({ status, league, onLeague, version }) {
  if (!status) return html`<div class="empty">Loading…</div>`;
  const teams = status.teams || {};
  const last = status.last || {};
  const rows = [
    ['Server day', dayLabel(status.day)],
    ['Auto refresh', status.auto ? 'on' : 'off'],
    ['Plan', status.plan_file || 'none saved yet'],
    ['Plan saved', status.plan_saved_at ? clock(status.plan_saved_at) : '—'],
    ...Object.entries(last).map(([step, when]) => [step[0].toUpperCase() + step.slice(1), clock(when)]),
  ];
  if (status.skip_snapshots) rows.push(['Reports', 'scheduled snapshots']);
  if (status.league_file) rows.push(['League', `${status.league_file} (not the platform)`]);
  return html`
    <div class="sec"><h2>League</h2></div>
    <div class="choices" role="group" aria-label="League">
      ${status.leagues.map((name) => html`
        <button type="button" class="choice" aria-pressed=${name === league} onClick=${() => onLeague(name)}>
          <span><span class="name">${(teams[name] && teams[name].team) || name}</span><br />
          <span class="sub">${name} · ${(teams[name] && teams[name].platform) || ''}</span></span>
          ${name === league && html`<span class="mark">Shown</span>`}
        </button>`)}
    </div>
    <div class="sec"><h2>Refresh log</h2><span class="note">tap one for its lines</span></div>
    <${RefreshLog} version=${version} />
    <div class="sec"><h2>Server</h2></div>
    <table class="kv"><tbody>
      ${rows.map(([k, v]) => html`<tr><th scope="row">${k}</th><td>${v}</td></tr>`)}
    </tbody></table>`;
}

function App() {
  const route = useRoute();
  const tab = route.tab;
  const [league, setLeague] = useState(() => store.get('fh.league'));
  const [status, setStatus] = useState(null);
  const [error, setError] = useState(null);
  const [job, setJob] = useState(null);
  const [now, setNow] = useState(() => new Date());
  const [version, setVersion] = useState(0);     // bumps when a refresh ends: screens re-fetch
  const tracking = useRef(null);

  const loadStatus = useCallback(async (name) => {
    try {
      let got;
      try {
        got = await api.status(name || undefined);
      } catch (e) {
        if (e.status !== 404 || !name) throw e;
        got = await api.status();             // a league this server doesn't plan: its first
      }
      setStatus(got);
      setError(null);
      setNow(new Date());
      if (got.league !== name) { setLeague(got.league); store.set('fh.league', got.league); }
      if (got.job && tracking.current !== got.job.id) track(got.job);
    } catch (e) {
      setError(e.message);
    }
  }, []);

  // Follow a job to its end: its lines as they come, then the status again.
  const track = useCallback(async (start) => {
    tracking.current = start.id;
    let current = { ...start, lines: start.lines || [] };
    setJob(current);
    setVersion((v) => v + 1);                // the refresh log shows it running
    while (current.state === 'running' && tracking.current === start.id) {
      await new Promise((r) => setTimeout(r, JOB_EVERY_MS));
      try {
        const got = await api.job(start.id, current.next || current.lines.length);
        current = { ...got, lines: current.lines.concat(got.lines) };
        setJob(current);
      } catch (e) {
        current = { ...current, state: 'failed', error: e.message };
        setJob(current);
      }
    }
    setVersion((v) => v + 1);
  }, []);

  useEffect(() => {
    loadStatus(league);
    const timer = setInterval(() => loadStatus(league), STATUS_EVERY_MS);
    const onShow = () => { if (document.visibilityState === 'visible') loadStatus(league); };
    document.addEventListener('visibilitychange', onShow);
    return () => { clearInterval(timer); document.removeEventListener('visibilitychange', onShow); };
  }, [league]);

  useEffect(() => { if (version) loadStatus(league); }, [version]);

  const chooseLeague = (name) => { store.set('fh.league', name); setLeague(name); setStatus(null); };
  const refresh = async (mode) => {
    try {
      track(await api.refresh(league, mode));
    } catch (e) {
      setError(e.message);
    }
  };
  const closeJob = () => { tracking.current = null; setJob(null); };

  // One game: full screen, as the design's game screens -- no header, no tabs.
  if (route.game && league) {
    return html`<div class="app"><${Guard} key=${location.hash}><${Game} league=${league} gameId=${route.game} view=${route.view} /></${Guard}></div>`;
  }
  // Games: its own header (the day, live), as the design's Games screen.
  if (tab === 'games' && league) {
    return html`<div class="app">
      <${JobStrip} job=${job} onClose=${closeJob} />
      <${Guard} key="games"><${Games} league=${league} version=${version} /></${Guard}>
      <${Tabs} tab=${tab} />
    </div>`;
  }
  return html`<div class="app">
    <${Header} status=${status} league=${league} onLeague=${chooseLeague} job=${job} onRefresh=${refresh} now=${now} />
    <${JobStrip} job=${job} onClose=${closeJob} />
    ${error && html`<div class="error" role="alert">${error}</div>`}
    <main class="screen"><${Guard} key=${tab}>
      ${tab === 'more'
        ? html`<${More} status=${status} league=${league} onLeague=${chooseLeague} version=${version} />`
        : tab === 'roster' && league
          ? html`<${Roster} league=${league} version=${version} />`
          : tab === 'ai' && league
            ? html`<${Suggestions} league=${league} version=${version} />`
            : tab === 'planner' && league
              ? html`<${Planner} league=${league} version=${version} />`
              : html`<${Coming} tab=${tab} />`}
    </${Guard}></main>
    <${Tabs} tab=${tab} />
  </div>`;
}

render(html`<${App} />`, document.getElementById('root'));

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('sw.js').catch(() => { /* the app works without it */ });
}
