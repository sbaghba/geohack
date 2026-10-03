// SiteSense frontend API client — the ONLY file that knows URLs and field names.
// Usage:
//   import * as api from './api.js';
//   api.configure({ mode: 'live', baseUrl: 'https://sitesense-api.example.com' });
//   const report = await api.analyze({ lat, lon, mw: 100, cooling: 'evaporative', power: 'grid', workload: 'ai' });
//
// Modes:
//   'mock' -> reads static files from mockBase (copy contract/mock/ into your site's public folder)
//   'live' -> calls the FastAPI backend (the stub server or the real one; same contract)
//
// All functions throw ApiError { error, detail, status } on failure.

const CONFIG = {
  mode: 'mock',
  baseUrl: 'http://localhost:8000',
  mockBase: './mock',
  timeoutMs: 15000,
};

export function configure(opts) { Object.assign(CONFIG, opts); }
export function getConfig() { return { ...CONFIG }; }

export class ApiError extends Error {
  constructor(error, detail, status) {
    super(`${error}: ${detail}`);
    this.error = error;     // 'invalid_request' | 'out_of_coverage' | 'upstream_unavailable' | 'rate_limited' | 'internal' | 'network'
    this.detail = detail;
    this.status = status;
  }
}

export const DEFAULT_CONFIG = { mw: 100, cooling: 'evaporative', power: 'grid', workload: 'ai' };

// ------------------------------------------------------------ helpers ---

async function request(path, { method = 'GET', body } = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), CONFIG.timeoutMs);
  let res;
  try {
    res = await fetch(CONFIG.baseUrl + path, {
      method,
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
      signal: ctrl.signal,
    });
  } catch (e) {
    throw new ApiError('network', e.name === 'AbortError' ? 'Request timed out' : e.message, 0);
  } finally {
    clearTimeout(timer);
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(data.error || 'internal', data.detail || res.statusText, res.status);
  return data;
}

async function mockFile(name) {
  const res = await fetch(`${CONFIG.mockBase}/${name}`);
  if (!res.ok) throw new ApiError('internal', `Mock file missing: ${name}`, res.status);
  return res.json();
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---------------------------------------------------------- endpoints ---

/** POST /api/analyze -> Report */
export async function analyze(req) {
  const body = { ...DEFAULT_CONFIG, ...req };
  if (CONFIG.mode === 'mock') {
    const r = await mockFile('analyze.json');
    await sleep(300);
    // keep the pin where the user dropped it
    return { ...r, request: body, site: { ...r.site, lat: body.lat, lon: body.lon } };
  }
  return request('/api/analyze', { method: 'POST', body });
}

/** POST /api/suggest -> SuggestResponse */
export async function suggest(req) {
  const body = { ...DEFAULT_CONFIG, radius_km: 50, n: 3, ...req };
  if (CONFIG.mode === 'mock') return mockFile('suggest.json');
  return request('/api/suggest', { method: 'POST', body });
}

/** GET /api/layers -> { layers: LayerInfo[] } */
export async function listLayers() {
  if (CONFIG.mode === 'mock') return mockFile('layers.json');
  return request('/api/layers');
}

/** GET /api/layers/{name} -> GeoJSON FeatureCollection, properties { hex_id, value } */
export async function getLayer(name) {
  if (CONFIG.mode === 'mock') return mockFile('layer_sample.geojson');
  return request(`/api/layers/${encodeURIComponent(name)}`);
}

/** GET /api/health -> { ok, llm_ok, model, grid_rows, contract_version } */
export async function health() {
  if (CONFIG.mode === 'mock') return { ok: true, llm_ok: false, model: null, grid_rows: 0, contract_version: 'mock' };
  return request('/api/health');
}

/**
 * POST /api/chat (Server-Sent Events over fetch)
 * @param {{messages: {role:'user'|'assistant', content:string}[], report?: object, config?: object}} body
 * @param {{onToken?, onToolCall?, onReport?, onSuggestions?, onDone?, onError?}} handlers
 * @returns {AbortController} call .abort() to stop the stream
 *
 * Event order example: tool_call -> report -> token... -> done
 * onReport(report): backend already re-ran analyze -> move the pin, update sliders from report.request, re-render.
 * onSuggestions(resp): drop ghost pins for resp.candidates.
 */
export function chat(body, handlers = {}) {
  const ctrl = new AbortController();
  const h = { onToken() {}, onToolCall() {}, onReport() {}, onSuggestions() {}, onDone() {}, onError() {}, ...handlers };
  const dispatch = (event, data) => {
    ({ token: () => h.onToken(data.text), tool_call: () => h.onToolCall(data), report: () => h.onReport(data),
       suggestions: () => h.onSuggestions(data), done: () => h.onDone(data), error: () => h.onError(data) }[event] || (() => {}))();
  };

  if (CONFIG.mode === 'mock') {
    mockChat(body, dispatch, ctrl.signal);
    return ctrl;
  }

  (async () => {
    try {
      const res = await fetch(CONFIG.baseUrl + '/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
        body: JSON.stringify(body),
        signal: ctrl.signal,
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        return dispatch('error', { message: err.detail || res.statusText });
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let cut;
        while ((cut = buf.indexOf('\n\n')) !== -1) {
          const block = buf.slice(0, cut);
          buf = buf.slice(cut + 2);
          let event = 'message';
          const dataLines = [];
          for (const line of block.split('\n')) {
            if (line.startsWith('event:')) event = line.slice(6).trim();
            else if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart());
          }
          if (dataLines.length) dispatch(event, JSON.parse(dataLines.join('\n')));
        }
      }
    } catch (e) {
      if (e.name !== 'AbortError') dispatch('error', { message: e.message });
    }
  })();
  return ctrl;
}

async function mockChat(body, dispatch, signal) {
  const last = body.messages[body.messages.length - 1].content.toLowerCase();
  const say = async (text) => {
    for (const w of text.split(' ')) { if (signal.aborted) return; dispatch('token', { text: w + ' ' }); await sleep(30); }
  };
  if (/move|east|west|north|south|try/.test(last)) {
    const cfg = body.config || body.report?.request || { lat: 35.655, lon: -78.462 };
    dispatch('tool_call', { id: 'tc1', name: 'move_site', args: { lat: cfg.lat, lon: cfg.lon + 0.2 } });
    dispatch('report', await analyze({ ...cfg, lon: cfg.lon + 0.2 }));
    await say('I moved the site about 18 km east. (Mock reply.)');
  } else if (/better|where|alternative/.test(last)) {
    dispatch('tool_call', { id: 'tc1', name: 'find_better_sites', args: { radius_km: 50 } });
    dispatch('suggestions', await suggest({}));
    await say('Here are three nearby sites with lower community burden. (Mock reply.)');
  } else {
    await say('This is the mock analyst. Ask me to move the site east, or where a better site would be.');
  }
  dispatch('done', { finish_reason: 'stop' });
}
