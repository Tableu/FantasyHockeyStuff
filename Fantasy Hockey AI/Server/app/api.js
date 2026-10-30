// The plan server's API (Server/server.py), same origin as the app. Every call but the jobs' and
// the login's takes ?league=; the session cookie rides along (auth.py).

export class ApiError extends Error {
  constructor(status, detail) {
    super(detail);
    this.status = status;
  }
}

// Whether the last data shown came from the app's saved copy (the server out of reach): every
// listener hears the time it was saved, or null once an answer comes from the server again.
const staleListeners = new Set();
export function onStale(listener) {
  staleListeners.add(listener);
  return () => staleListeners.delete(listener);
}

// Whether the server wants a login (auth.py): every listener hears true on a 401 from any call
// but the login itself.
const authListeners = new Set();
export function onAuth(listener) {
  authListeners.add(listener);
  return () => authListeners.delete(listener);
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
  if (response.status === 401 && path !== '/login') authListeners.forEach((listener) => listener(true));
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const data = await response.json();
      if (data && data.detail) detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
    } catch (_) { /* not JSON */ }
    throw new ApiError(response.status, detail);
  }
  const savedAt = response.headers.get('X-Saved-At');
  staleListeners.forEach((listener) => listener(savedAt));
  return response.json();
}

export const api = {
  login: (password) => call('POST', '/login', { body: { password } }),
  logout: () => call('POST', '/logout', { body: {} }),
  status: (league) => call('GET', '/status', { league }),
  // scope 'league': this league alone (the app's taps); the server's default for full / quick is all.
  refresh: (league, mode, scope = 'league') => call('POST', '/refresh', { league, body: { mode, scope } }),
  job: (id, after = 0) => call('GET', `/jobs/${encodeURIComponent(id)}`, { query: { after } }),
  jobs: () => call('GET', '/jobs'),
  roster: (league, day) => call('GET', '/roster', { league, query: day ? { day } : {} }),
  tonight: (league) => call('GET', '/tonight', { league }),
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
  games: (league) => call('GET', '/games', { league }),
  goals: (league) => call('GET', '/goals', { league }),
  game: (league, id) => call('GET', `/games/${id}`, { league }),
  lines: (league, id) => call('GET', `/games/${id}/lines`, { league }),
  matchup: (league) => call('GET', '/matchup', { league }),
};
