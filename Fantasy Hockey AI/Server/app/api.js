// The plan server's API (Server/server.py), same origin as the app. Every call but the jobs' and
// the leagues' takes ?league=; this browser's cookie rides along (auth.py) and names its leagues.

export class ApiError extends Error {
  constructor(status, detail, data) {
    super(detail);
    this.status = status;
    this.data = data || {};       // the server's detail when it is an object: {add}, {needs_cookies}
  }
}

// The server's answer when this browser has no league yet: the app shows the add-league screen.
export const needsLeague = (error) => !!(error && error.status === 404 && error.data && error.data.add);

// Whether the last data shown came from the app's saved copy (the server out of reach): every
// listener hears the time it was saved, or null once an answer comes from the server again.
const staleListeners = new Set();
export function onStale(listener) {
  staleListeners.add(listener);
  return () => staleListeners.delete(listener);
}

async function call(method, path, { league, body, query } = {}) {
  const params = new URLSearchParams(query || {});
  if (league) params.set('league', league);
  const qs = params.toString();
  let response;
  try {
    response = await fetch(path + (qs ? `?${qs}` : ''), {
      method,
      headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: 'no-store',
    });
  } catch (error) {
    throw new ApiError(0, "Can't reach the server");
  }
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    let extra = null;
    try {
      const data = await response.json();
      if (data && typeof data.detail === 'string') detail = data.detail;
      else if (data && data.detail && typeof data.detail === 'object') {
        extra = data.detail;
        detail = typeof extra.detail === 'string' ? extra.detail : JSON.stringify(extra);
      }
    } catch (_) { /* not JSON */ }
    throw new ApiError(response.status, detail, extra);
  }
  const savedAt = response.headers.get('X-Saved-At');
  staleListeners.forEach((listener) => listener(savedAt));
  return response.json();
}

export const api = {
  forget: () => call('POST', '/forget', { body: {} }),
  leagues: () => call('GET', '/leagues'),
  // {url, espn_s2?, swid?} -> the league's teams and settings; a 401 with needs_cookies: a private ESPN league.
  previewLeague: (body) => call('POST', '/leagues/preview', { body }),
  addLeague: (body) => call('POST', '/leagues', { body }),
  removeLeague: (name) => call('DELETE', `/leagues/${encodeURIComponent(name)}`),
  status: (league) => call('GET', '/status', { league }),
  refresh: (league, mode) => call('POST', '/refresh', { league, body: { mode } }),
  job: (id, after = 0) => call('GET', `/jobs/${encodeURIComponent(id)}`, { query: { after } }),
  jobs: () => call('GET', '/jobs'),
  roster: (league) => call('GET', '/roster', { league }),
  players: (league) => call('GET', '/players', { league }),
  suggestions: (league, week) => call('GET', '/suggestions', { league, query: { week } }),
  pick: (league, body) => call('POST', '/choices/pick', { league, body }),
  together: (league, body) => call('POST', '/choices/together', { league, body }),
  // show: a week plan's label, drawn over your week (the Plan tab's chips).
  week: (league, week, show) => call('GET', '/week', { league, query: { week, ...(show ? { show } : {}) } }),
  picker: (league, week, day, occupant, slot) => call('GET', '/week/picker',
    { league, query: { week, day, slot, ...(occupant ? { occupant } : {}) } }),
  drops: (league, week, day, add, occupant) => call('GET', '/week/drops',
    { league, query: { week, day, add, ...(occupant ? { occupant } : {}) } }),
  weekEdit: (league, body) => call('POST', '/week/edit', { league, body }),
  games: (league, date) => call('GET', '/games', { league, query: date ? { date } : {} }),
  gamesWeek: (league) => call('GET', '/games/week', { league }),
  goals: (league) => call('GET', '/goals', { league }),
  game: (league, id) => call('GET', `/games/${id}`, { league }),
  lines: (league, id) => call('GET', `/games/${id}/lines`, { league }),
  matchup: (league) => call('GET', '/matchup', { league }),
  matchupPlayers: (league) => call('GET', '/matchup/players', { league }),
};
