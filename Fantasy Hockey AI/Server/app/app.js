// The phone app's shell (plan P0): header with the league, the day, how fresh each step is and
// the gear to More; the running refresh's progress; the bottom tabs. Roster is roster.js (P1),
// Players is players.js (redesign step 2), Plan is plan.js (redesign step 3: the Planner and AI in one), Games and a game's screens are games.js (P4),
// Matchup is matchup.js (P5). Everything shown comes from the plan server (api.js);
// nothing is computed here. A window WIDE_PX or wider (layout.js) gets the PC layout: the tabs as a
// sidebar, and Games as a list, the chosen game and the goal feed side by side.
import { html, render, useState, useEffect, useRef, useCallback, useErrorBoundary } from './vendor/htm-preact-3.1.1.mjs';
import { api, onStale, needsLeague } from './api.js';
import { Roster } from './roster.js';
import { Players } from './players.js';
import { Plan } from './plan.js';
import { Games, Game } from './games.js';
import { Matchup } from './matchup.js';
import { useWide, useEscape, REFRESH_EVENT } from './layout.js';

const TABS = [
  ['roster', 'Roster'], ['players', 'Players'], ['plan', 'Plan'], ['games', 'Games'], ['matchup', 'Matchup'],
];
// More is the header's gear (the redesign), not a tab.
const ROUTES = [...TABS.map(([id]) => id), 'more'];
const GEAR = html`<svg aria-hidden="true" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"></path><circle cx="12" cy="12" r="3"></circle></svg>`;
// /status `last` steps, in the header's order, with their short names.
const STEPS = [['injuries', 'Inj'], ['goalies', 'Gl'], ['projections', 'Proj'], ['league read', 'Lg'], ['lines', 'Lines']];
const MODE_LABELS = { full: 'Full', quick: 'Quick', news: 'News', league: 'League', plan: 'Re-plan',
  nightly: 'Nightly import', player_teams: 'Player teams', injuries: 'Injury listing',
  settings_check: 'Settings check', daily_plans: 'Daily plans' };
// A job still to finish: running, or queued behind another (the server runs one at a time).
const active = (job) => !!job && (job.state === 'running' || job.state === 'queued');
const STALE_MIN = 120;          // a step older than this shows amber
const PULL_PX = 70;             // how far down a pull at the top of a screen must go to refresh
const STATUS_EVERY_MS = 60000;  // follows the server's refreshes (the daily pass, other users' taps)
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

// The time the data shown was saved while the server is out of reach (api.js onStale), else null.
function useStale() {
  const [savedAt, setSavedAt] = useState(null);
  useEffect(() => onStale(setSavedAt), []);
  return savedAt;
}

function StaleBanner({ savedAt }) {
  if (!savedAt) return null;
  const at = new Date(savedAt);
  const sameDay = at.toDateString() === new Date().toDateString();
  const when = at.toLocaleString('en-US', sameDay ? { hour: 'numeric', minute: '2-digit' }
    : { weekday: 'short', hour: 'numeric', minute: '2-digit' });
  return html`<div class="stale" role="status">Offline · showing what was saved at ${when}</div>`;
}

// Pull down at the top of the screen to run `onPull` (Quick). Engages only on a mostly vertical
// drag from the top, so a sideways swipe of a wide table still scrolls it. Returns how far it is
// pulled, for the indicator.
function usePull(ref, onPull, enabled) {
  const [pull, setPull] = useState(0);
  const latest = useRef(onPull);
  latest.current = onPull;
  useEffect(() => {
    const el = ref.current;
    if (!el || !enabled) return undefined;
    let start = null, dist = 0, engaged = false;
    // At the top: the screen, and any box inside it the touch starts in (a table that scrolls on
    // its own, its header held at its top) -- a pull down in a scrolled table scrolls it back.
    const atTop = (target) => {
      for (let n = target; n && n !== el; n = n.parentElement) if (n.scrollTop > 0) return false;
      return el.scrollTop <= 0;
    };
    const down = (e) => {
      start = atTop(e.target) && e.touches.length === 1 ? { x: e.touches[0].clientX, y: e.touches[0].clientY } : null;
      dist = 0; engaged = false;
    };
    const move = (e) => {
      if (!start) return;
      const dx = e.touches[0].clientX - start.x, dy = e.touches[0].clientY - start.y;
      if (!engaged) {
        if (Math.abs(dx) > 10 && Math.abs(dx) >= dy) { start = null; return; }   // a sideways swipe
        if (dy < 10) return;
        engaged = true;
      }
      dist = Math.max(0, dy);
      setPull(Math.min(dist, PULL_PX * 1.6));
      if (e.cancelable) e.preventDefault();
    };
    const up = () => {
      if (engaged && dist >= PULL_PX) latest.current();
      start = null; dist = 0; engaged = false;
      setPull(0);
    };
    el.addEventListener('touchstart', down, { passive: true });
    el.addEventListener('touchmove', move, { passive: false });
    el.addEventListener('touchend', up);
    el.addEventListener('touchcancel', up);
    return () => {
      el.removeEventListener('touchstart', down);
      el.removeEventListener('touchmove', move);
      el.removeEventListener('touchend', up);
      el.removeEventListener('touchcancel', up);
    };
  }, [enabled]);
  return pull;
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
    // The Planner and AI tabs are Plan now: their old links land there.
    if (path === 'planner' || path === 'ai') return { tab: 'plan' };
    return { tab: ROUTES.includes(path) ? path : 'roster' };
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

function Header({ status, league, onLeague, job, onRefresh, now, wide, tab }) {
  const [open, setOpen] = useState(false);
  const teams = (status && status.teams) || {};
  const leagues = (status && status.leagues) || [];
  const team = (teams[league] && teams[league].team) || league || 'Fantasy Hockey';
  const running = active(job);
  useEscape(open, () => setOpen(false));
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
          ${wide && html`<button type="button" class="btn small" disabled=${running} onClick=${() => onRefresh('quick')}
            title="Quick: injuries, goalies and the roster, for this league (the rest are in More)">Refresh</button>`}
          <a href="#/more" class="gear" aria-current=${tab === 'more' ? 'page' : undefined}
            aria-label="More: refresh, leagues and settings" title="More">${GEAR}</a>
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
  if (active(job)) {
    return html`<div class="job" role="status"><span class="spin" aria-hidden="true"></span>
      <span>${MODE_LABELS[job.mode] || job.mode}</span>
      <span class="line">${job.state === 'queued' ? 'waiting for the refresh before it…' : last}</span></div>`;
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

// The bottom tabs on a phone; on a PC (`side`) the same links as a sidebar.
function Tabs({ tab, side }) {
  return html`<nav class=${side ? 'sidebar' : 'tabs'} aria-label="Sheets">
    ${side && html`<div class="brand">Fantasy Hockey</div>`}
    ${TABS.map(([id, label]) => html`<a href=${`#/${id}`} aria-current=${id === tab ? 'page' : undefined}>${label}</a>`)}
  </nav>`;
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
        <span class="what">${MODE_LABELS[job.mode] || job.mode} · <span class=${`state-${job.state}`}>${job.state}</span></span>
        <span class="when">${clock(job.started)}${secs !== null ? ` · ${secs} s` : ''}</span>
        <span class="by">${job.by}</span>
        ${job.error && html`<span class="err">${job.error}</span>`}
      </button>
      ${open === job.id && html`<pre class="lines">${(lines[job.id] || ['…']).join('\n')}</pre>`}`;
  })}</div>`;
}

// Adding a league (server.py POST /leagues): the league's link, then -- a private ESPN league --
// its cookies, then which team is yours. Shown when this browser has no league yet, and from More.
// The browser's cookie is all that ties the leagues to it (auth.py): no account, no password.
function AddLeague({ onAdded, onCancel }) {
  const [url, setUrl] = useState('');
  const [s2, setS2] = useState('');
  const [swid, setSwid] = useState('');
  const [needCookies, setNeedCookies] = useState(false);
  const [preview, setPreview] = useState(null);
  const [team, setTeam] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const ask = () => ({ url: url.trim(), ...(s2 && swid ? { espn_s2: s2.trim(), swid: swid.trim() } : {}) });
  const lookUp = async (e) => {
    e.preventDefault();
    if (!url.trim() || busy) return;
    setBusy(true); setError(null); setPreview(null); setTeam(null);
    try {
      setPreview(await api.previewLeague(ask()));
    } catch (err) {
      if (err.status === 401 && err.data.needs_cookies) setNeedCookies(true);
      setError(err.status === 0 ? "Can't reach the server" : err.message);
    } finally {
      setBusy(false);
    }
  };
  const add = async () => {
    if (team === null || busy) return;
    setBusy(true); setError(null);
    try {
      const got = await api.addLeague({ ...ask(), team_id: team });
      onAdded(got.league, got.job);
    } catch (err) {
      setError(err.status === 0 ? "Can't reach the server" : err.message);
      setBusy(false);
    }
  };
  const sum = preview && preview.summary;
  return html`<div class="app"><div class="login add-league">
    <h1>Fantasy Hockey</h1>
    <form onSubmit=${lookUp} class="add-step">
      <label for="league-url">Your league's page</label>
      <input id="league-url" type="url" inputmode="url" autocomplete="off" autofocus
        placeholder="fleaflicker.com/nhl/leagues/… or fantasy.espn.com/hockey/league?leagueId=…"
        value=${url} onInput=${(e) => { setUrl(e.currentTarget.value); setPreview(null); }} />
      ${needCookies && html`
        <p class="note">This ESPN league is private. While logged in to ESPN, open your browser's
          developer tools → Application (Storage) → Cookies → espn.com, and copy <b>espn_s2</b> and <b>SWID</b>.
          They let this server read your league (it never changes anything); they stop working when you log out of ESPN.</p>
        <label for="espn-s2">espn_s2</label>
        <input id="espn-s2" autocomplete="off" value=${s2} onInput=${(e) => setS2(e.currentTarget.value)} />
        <label for="espn-swid">SWID</label>
        <input id="espn-swid" autocomplete="off" placeholder="{…}" value=${swid} onInput=${(e) => setSwid(e.currentTarget.value)} />`}
      <button type="submit" class="btn" disabled=${busy || !url.trim() || (needCookies && !(s2 && swid))}>
        ${busy && !preview ? 'Looking it up…' : 'Look it up'}</button>
    </form>
    ${error && html`<div class="error" role="alert">${error}</div>`}
    ${preview && html`
      <div class="add-step">
        <p class="note">${sum.teams} teams · ${sum.slots}${sum.bench != null ? ` · bench ${sum.bench}` : ''}${sum.moves_per_week ? ` · ${sum.moves_per_week >= 99 ? 'unlimited' : sum.moves_per_week} moves a week` : ''}<br />${sum.points}</p>
        ${preview.problem
          ? html`<div class="error" role="alert">Can't plan this league: ${preview.problem}</div>`
          : html`
            <label>Which team is yours?</label>
            <div class="choices" role="radiogroup" aria-label="Your team">
              ${preview.teams.map((t) => html`
                <button type="button" class="choice" role="radio" aria-checked=${team === t.id} aria-pressed=${team === t.id}
                  onClick=${() => setTeam(t.id)}>
                  <span><span class="name">${t.name}</span>
                  ${t.linked && html`<br /><span class="sub">Someone added this team already: you'll share its picks</span>`}</span>
                  ${team === t.id && html`<span class="mark">Yours</span>`}
                </button>`)}
            </div>
            <button type="button" class="btn primary" disabled=${busy || team === null} onClick=${add}>
              ${busy ? 'Adding…' : 'Add league'}</button>`}
      </div>`}
    <p class="note">Your leagues are kept for this browser only, by a cookie: no account or password.
      Clear this site's cookies and you'll add them again.</p>
    ${onCancel && html`<button type="button" class="btn" onClick=${onCancel}>Cancel</button>`}
  </div></div>`;
}

// The refreshes More offers, each for the league on screen (the daily pass does every league once a day).
const REFRESHES = [
  ['league', 'League', 'Made a move? Read the roster again', '~7 s'],
  ['news', 'News', 'Injuries and goalies, on the roster as last read', '~12 s'],
  ['quick', 'Quick', 'News and the roster', '~12 s'],
  ['full', 'Full', 'Everything, line charts too', '~20 s'],
];

function More({ status, league, onLeague, version, onLogout, onRefresh, running, onAdd, onRemove }) {
  if (!status) return html`<div class="empty">Loading…</div>`;
  const teams = status.teams || {};
  const last = status.last || {};
  const rows = [
    ['Server day', dayLabel(status.day)],
    ['Daily plan', `${status.daily ? `${status.daily.lead_min} min before the first game, by ${status.daily.latest}` : '—'}`
      + `${status.daily && status.daily.last ? ` · last ${new Date(status.daily.last).toLocaleString('en-US', { weekday: 'short', hour: 'numeric', minute: '2-digit' })}` : ''}`],
    ['Plan', status.plan_file || 'none saved yet'],
    ['Plan saved', status.plan_saved_at ? clock(status.plan_saved_at) : '—'],
    ...Object.entries(last).map(([step, when]) => [step[0].toUpperCase() + step.slice(1), clock(when)]),
  ];
  if (status.skip_snapshots) rows.push(['Reports', 'scheduled snapshots']);
  // The daily jobs the server runs (scheduled.py), when it runs them: their times and last run.
  for (const job of Object.values(status.scheduled || {})) {
    const times = job.times.map((t) => {
      const [h, m] = t.split(':').map(Number);
      const at = h * 60 + m + (job.shift_min || 0);
      return `${String(Math.floor(at / 60) % 24).padStart(2, '0')}:${String(at % 60).padStart(2, '0')}`;
    }).join(', ');
    rows.push([job.label, `${times} · last ${job.last ? new Date(job.last).toLocaleString('en-US', { weekday: 'short', hour: 'numeric', minute: '2-digit' }) : 'never'}`]);
  }
  if (status.league_file) rows.push(['League', `${status.league_file} (not the platform)`]);
  return html`<div class="more">
    <div class="more-refresh">
    <div class="sec"><h2>Refresh</h2><span class="note">this league${status.tap_wait_s ? ` · again in ${Math.ceil(status.tap_wait_s / 60)} min` : ''}<span class="pull-hint"> · pull down = Quick</span></span></div>
    <div class="choices" role="group" aria-label="Refresh">
      ${REFRESHES.map(([mode, label, what, takes]) => html`
        <button type="button" class="choice" disabled=${running} onClick=${() => onRefresh(mode)}>
          <span><span class="name">${label}</span><br /><span class="sub">${what}</span></span>
          <span class="sub">${takes}</span>
        </button>`)}
    </div>
    </div>
    <div class="more-league">
    <div class="sec"><h2>League</h2></div>
    <div class="choices" role="group" aria-label="League">
      ${status.leagues.map((name) => html`
        <div class="league-row">
          <button type="button" class="choice" aria-pressed=${name === league} onClick=${() => onLeague(name)}>
            <span><span class="name">${(teams[name] && teams[name].team) || name}</span><br />
            <span class="sub">${name} · ${(teams[name] && teams[name].platform) || ''}</span></span>
            ${name === league && html`<span class="mark">Shown</span>`}
          </button>
          <button type="button" class="btn small remove" onClick=${() => onRemove(name)}
            aria-label=${`Remove ${(teams[name] && teams[name].team) || name}`}>Remove</button>
        </div>`)}
    </div>
    <div style="padding: 10px 12px"><button type="button" class="btn" onClick=${onAdd}>Add a league</button></div>
    </div>
    <div class="more-log">
    <div class="sec"><h2>Refresh log</h2><span class="note">tap one for its lines</span></div>
    <${RefreshLog} version=${version} />
    </div>
    <div class="more-server">
    <div class="sec"><h2>Server</h2></div>
    <table class="kv"><tbody>
      ${rows.map(([k, v]) => html`<tr><th scope="row">${k}</th><td>${v}</td></tr>`)}
    </tbody></table>
    </div>
    <div class="more-device">
    <div class="sec"><h2>This browser</h2></div>
    <p class="empty" style="padding-top: 0; padding-bottom: 8px">Your leagues are kept for this browser by a cookie,
      not an account: forget it (or clear this site's cookies) and you'll add them again.</p>
    <div style="padding: 0 12px 16px"><button type="button" class="btn" onClick=${onLogout}>Forget this browser</button></div>
    </div>
  </div>`;
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
  const [noLeague, setNoLeague] = useState(false); // this browser has no league yet: the add screen
  const [adding, setAdding] = useState(false);     // More's "Add a league"
  const wide = useWide();
  const savedAt = useStale();
  const screen = useRef(null);
  const tracking = useRef(null);

  const loadStatus = useCallback(async (name) => {
    try {
      let got;
      try {
        got = await api.status(name || undefined);
      } catch (e) {
        if (e.status !== 404 || !name || needsLeague(e)) throw e;
        got = await api.status();             // a league this browser doesn't have: its first
      }
      setNoLeague(false);
      setStatus(got);
      setError(null);
      setNow(new Date());
      if (got.league !== name) { setLeague(got.league); store.set('fh.league', got.league); }
      if (got.job && got.job.mine && tracking.current !== got.job.id) track(got.job);
    } catch (e) {
      if (needsLeague(e)) { setNoLeague(true); setStatus(null); return; }
      setError(e.message);
    }
  }, []);

  // Follow a job to its end: its lines as they come, then the status again.
  const track = useCallback(async (start) => {
    tracking.current = start.id;
    let current = { ...start, lines: start.lines || [] };
    setJob(current);
    setVersion((v) => v + 1);                // the refresh log shows it running
    while (active(current) && tracking.current === start.id) {
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
  const running = active(job);
  const refreshRef = useRef(refresh);
  refreshRef.current = refresh;
  useEffect(() => {
    const on = (e) => refreshRef.current((e.detail && e.detail.mode) || 'quick');
    addEventListener(REFRESH_EVENT, on);
    return () => removeEventListener(REFRESH_EVENT, on);
  }, []);
  const closeJob = () => { tracking.current = null; setJob(null); };
  // A league added: shown, and its first read and plan followed.
  const added = (name, started) => {
    store.set('fh.league', name);
    setAdding(false); setNoLeague(false); setError(null); setStatus(null);
    setLeague(name);
    if (started) track(started);
    location.hash = '#/roster';
  };
  const remove = async (name) => {
    try {
      const got = await api.removeLeague(name);
      const next = (got.leagues || [])[0] || null;
      store.set('fh.league', next || '');
      setStatus(null);
      setLeague(next);
      if (!next) setNoLeague(true);
      setVersion((v) => v + 1);
    } catch (e) {
      setError(e.message);
    }
  };
  // Forgotten: the cookie and the saved screens go, so the browser keeps no league data.
  const logout = async () => {
    try { await api.forget(); } catch (_) { /* out of reach: the copy still goes */ }
    try { await caches.delete('fh-data-v1'); } catch (_) { /* no Cache API */ }
    store.set('fh.league', '');
    setStatus(null); setLeague(null); setJob(null); setNoLeague(true);
  };
  const pull = usePull(screen, () => refresh('quick'), !!league && !wide && !route.game && tab !== 'games'
    && !running);

  if (noLeague || adding) return html`<${AddLeague} onAdded=${added} onCancel=${adding && !noLeague ? () => setAdding(false) : null} />`;
  if (wide) {
    // The PC layout: the sidebar, then the header and the screen. Games keeps its list, the chosen
    // game and the goal feed side by side, each scrolling on its own, as the Planner does.
    const games = tab === 'games' || !!route.game;
    const fill = games || tab === 'plan' || tab === 'players';
    return html`<div class="app wide">
      <${Tabs} tab=${tab} side />
      <div class="col">
        <${Header} status=${status} league=${league} onLeague=${chooseLeague} job=${job} onRefresh=${refresh} now=${now} wide=${wide} tab=${tab} />
        <${JobStrip} job=${job} onClose=${closeJob} />
        <${StaleBanner} savedAt=${savedAt} />
        ${error && !savedAt && html`<div class="error" role="alert">${error}</div>`}
        <main class=${`screen ${fill ? 'fill' : ''}`} ref=${screen}>
          <${Guard} key=${games ? 'games' : tab}>
          ${!league ? html`<div class="empty">Loading…</div>`
            : games ? html`<${Games} league=${league} version=${version} wide selected=${route.game || null} view=${route.view || 'summary'} />`
              : tab === 'more' ? html`<${More} status=${status} league=${league} onLeague=${chooseLeague} version=${version} onLogout=${logout} onRefresh=${refresh} running=${running} onAdd=${() => setAdding(true)} onRemove=${remove} />`
                : tab === 'players' ? html`<${Players} league=${league} version=${version} wide />`
                  : tab === 'plan' ? html`<${Plan} league=${league} version=${version} wide />`
                    : tab === 'matchup' ? html`<${Matchup} league=${league} version=${version} />`
                      : html`<${Roster} league=${league} version=${version} wide />`}
          </${Guard}>
        </main>
      </div>
    </div>`;
  }
  // One game: full screen, as the design's game screens -- no header, no tabs.
  if (route.game && league) {
    return html`<div class="app"><${Guard} key=${location.hash}><${Game} league=${league} gameId=${route.game} view=${route.view} /></${Guard}></div>`;
  }
  // Games: the app's header with the team selector (the user, 2026-10-20), no pull to refresh.
  if (tab === 'games' && league) {
    return html`<div class="app">
      <${Header} status=${status} league=${league} onLeague=${chooseLeague} job=${job} onRefresh=${refresh} now=${now} wide=${wide} tab=${tab} />
      <${JobStrip} job=${job} onClose=${closeJob} />
      <${StaleBanner} savedAt=${savedAt} />
      <${Guard} key="games"><${Games} league=${league} version=${version} /></${Guard}>
      <${Tabs} tab=${tab} />
    </div>`;
  }
  return html`<div class="app">
    <${Header} status=${status} league=${league} onLeague=${chooseLeague} job=${job} onRefresh=${refresh} now=${now} wide=${wide} tab=${tab} />
    <${JobStrip} job=${job} onClose=${closeJob} />
    <${StaleBanner} savedAt=${savedAt} />
    ${error && !savedAt && html`<div class="error" role="alert">${error}</div>`}
    <main class="screen" ref=${screen}>
      ${pull > 0 && html`<div class="pull" style=${`height: ${pull}px`} aria-hidden="true">
        <span>${pull >= PULL_PX ? 'Release to refresh' : 'Pull to refresh'}</span></div>`}
      <${Guard} key=${tab}>
      ${tab === 'more'
        ? html`<${More} status=${status} league=${league} onLeague=${chooseLeague} version=${version} onLogout=${logout} onRefresh=${refresh} running=${running} onAdd=${() => setAdding(true)} onRemove=${remove} />`
        : tab === 'roster' && league
          ? html`<${Roster} league=${league} version=${version} />`
          : tab === 'players' && league
            ? html`<${Players} league=${league} version=${version} />`
            : tab === 'plan' && league
              ? html`<${Plan} league=${league} version=${version} />`
              : tab === 'matchup' && league
                ? html`<${Matchup} league=${league} version=${version} />`
                : html`<div class="empty">Loading…</div>`}
    </${Guard}></main>
    <${Tabs} tab=${tab} />
  </div>`;
}

render(html`<${App} />`, document.getElementById('root'));

if ('serviceWorker' in navigator) {
  // A new version's worker takes over (it skips waiting) while this page shows the old files:
  // reload once, so an update shows on the first open after it rather than the second.
  if (navigator.serviceWorker.controller) {
    let reloaded = false;
    navigator.serviceWorker.addEventListener('controllerchange', () => {
      if (!reloaded) { reloaded = true; location.reload(); }
    });
  }
  navigator.serviceWorker.register('sw.js').catch(() => { /* the app works without it */ });
}
