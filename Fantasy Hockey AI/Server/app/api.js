// The plan server's API (Server/server.py), same origin as the app. Every call but the jobs'
// takes ?league=.

export class ApiError extends Error {
  constructor(status, detail) {
    super(detail);
    this.status = status;
  }
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
    try {
      const data = await response.json();
      if (data && data.detail) detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
    } catch (_) { /* not JSON */ }
    throw new ApiError(response.status, detail);
  }
  return response.json();
}

export const api = {
  status: (league) => call('GET', '/status', { league }),
  refresh: (league, mode) => call('POST', '/refresh', { league, body: { mode } }),
  job: (id, after = 0) => call('GET', `/jobs/${encodeURIComponent(id)}`, { query: { after } }),
  jobs: () => call('GET', '/jobs'),
  roster: (league) => call('GET', '/roster', { league }),
  tonight: (league) => call('GET', '/tonight', { league }),
  suggestions: (league, week) => call('GET', '/suggestions', { league, query: { week } }),
  pick: (league, body) => call('POST', '/choices/pick', { league, body }),
};
