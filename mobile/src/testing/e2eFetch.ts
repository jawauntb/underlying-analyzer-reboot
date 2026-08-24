import { MOBILE_AGENT_TOOLS } from '@/src/api/agentTools';
import { API_ENDPOINTS } from '@/src/api/endpoints';
import { LENS_AUCTION_PERIODS } from '@/src/features/lens/lens-model';

type FetchLike = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;
type JsonObject = Record<string, unknown>;
type FixtureState = { researchAttempts: number };

const FIXTURE_MODEL = 'undercurrent-e2e-fixture';
const FIXTURE_PROVIDER = 'Undercurrent deterministic fixture';
const FIXTURE_SYMBOLS = ['AAPL', 'MSFT', 'NVDA'] as const;
const encoder = new TextEncoder();

function abortError(): Error {
  const error = new Error('The deterministic fixture request was aborted.');
  error.name = 'AbortError';
  return error;
}

function requestUrl(input: RequestInfo | URL): URL {
  if (input instanceof URL) return input;
  if (typeof input === 'string') return new URL(input);
  return new URL(input.url);
}

function requestMethod(input: RequestInfo | URL, init?: RequestInit): string {
  const inherited = typeof input === 'object' && !(input instanceof URL) && 'method' in input
    ? input.method
    : undefined;
  return (init?.method ?? inherited ?? 'GET').toUpperCase();
}

function jsonBody(init?: RequestInit): JsonObject {
  if (typeof init?.body !== 'string') throw new Error('[E2E fixture] Expected a JSON request body.');
  let parsed: unknown;
  try {
    parsed = JSON.parse(init.body);
  } catch {
    throw new Error('[E2E fixture] Request body is not valid JSON.');
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    throw new Error('[E2E fixture] Request body must be a JSON object.');
  }
  return parsed as JsonObject;
}

function headers(contentType: string): Headers {
  return {
    get: (name: string) => name.toLowerCase() === 'content-type' ? contentType : null,
  } as Headers;
}

function jsonResponse(payload: unknown, status = 200): Response {
  const raw = JSON.stringify(payload);
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: headers('application/json; charset=utf-8'),
    body: null,
    json: async () => JSON.parse(raw) as unknown,
    text: async () => raw,
  } as unknown as Response;
}

function streamResponse(payload: string, signal: AbortSignal | null, staysOpen: boolean): Response {
  const bytes = encoder.encode(payload);
  let delivered = false;
  let released = false;
  let pendingReject: ((reason: unknown) => void) | null = null;
  const rejectPending = () => {
    pendingReject?.(abortError());
    pendingReject = null;
  };
  signal?.addEventListener('abort', rejectPending, { once: true });

  const reader = {
    read: async (): Promise<ReadableStreamReadResult<Uint8Array>> => {
      if (released) return { done: true, value: undefined };
      if (signal?.aborted) throw abortError();
      if (!delivered) {
        delivered = true;
        return { done: false, value: bytes };
      }
      if (!staysOpen) return { done: true, value: undefined };
      return await new Promise<ReadableStreamReadResult<Uint8Array>>((_resolve, reject) => {
        pendingReject = reject;
        if (signal?.aborted) rejectPending();
      });
    },
    releaseLock: () => {
      released = true;
      signal?.removeEventListener('abort', rejectPending);
      pendingReject = null;
    },
  };

  return {
    ok: true,
    status: 200,
    headers: headers('application/x-ndjson; charset=utf-8'),
    body: { getReader: () => reader } as ReadableStream<Uint8Array>,
    json: async () => { throw new Error('The E2E research response is a stream.'); },
    text: async () => payload,
  } as unknown as Response;
}

function expectAapl(body: JsonObject): void {
  if (body.ticker !== 'AAPL') throw new Error('[E2E fixture] This proof supports the AAPL Lens only.');
}

function expectExactResearchBoundary(body: JsonObject): void {
  if (body.tool_policy !== 'exact') {
    throw new Error('[E2E fixture] Research requires the exact tool policy.');
  }
  const tools = body.tools;
  const exactTools = Array.isArray(tools)
    && tools.length === MOBILE_AGENT_TOOLS.length
    && new Set(tools).size === MOBILE_AGENT_TOOLS.length
    && MOBILE_AGENT_TOOLS.every((tool) => tools.includes(tool));
  if (!exactTools) throw new Error('[E2E fixture] Research requires the exact bounded-tool boundary.');
  if (body.required_first_tool !== 'ticker_research_bundle') {
    throw new Error('[E2E fixture] Research requires ticker_research_bundle before any other tool.');
  }
  if (body.context !== 'Ticker: AAPL\nPeriod: 1y') {
    throw new Error('[E2E fixture] Research context must be exactly AAPL over 1y.');
  }
  if (!Array.isArray(body.messages) || body.messages.length === 0) {
    throw new Error('[E2E fixture] Research requires at least one message.');
  }
}

function toolCatalog() {
  return {
    agent_ready: true,
    model: FIXTURE_MODEL,
    tool_count: MOBILE_AGENT_TOOLS.length,
    tools: MOBILE_AGENT_TOOLS.map((name) => ({
      name,
      title: name.split('_').map((word) => word[0].toUpperCase() + word.slice(1)).join(' '),
      group: 'research',
      summary: `Deterministic ${name} result for simulator proof.`,
      when_to_use: 'Use only inside the bounded mobile research run.',
      returns: 'A deterministic metadata-only result.',
      cost: 'fixture',
      produces_images: false,
      agent: true,
      mcp: false,
      http: { method: 'POST', path: `/api/tools/${name}` },
      arguments: ['ticker'],
      required: ['ticker'],
    })),
  };
}

function searchPayload(url: URL) {
  const query = url.searchParams.get('q')?.trim() ?? '';
  if (!query) throw new Error('[E2E fixture] Search requires a query.');
  const matchesApple = 'apple'.includes(query.toLowerCase()) || 'aapl'.startsWith(query.toLowerCase());
  return {
    query,
    results: matchesApple ? [{ symbol: 'AAPL', name: 'Apple Inc.', exchange: 'NASDAQ', asset_type: 'equity' }] : [],
    provider: FIXTURE_PROVIDER,
  };
}

function pulsePayload(body: JsonObject) {
  const requestedTickers = body.ticker === 'AAPL' ? ['AAPL'] : body.tickers;
  const exactDefault = Array.isArray(requestedTickers)
    && requestedTickers.length === FIXTURE_SYMBOLS.length
    && FIXTURE_SYMBOLS.every((ticker, index) => requestedTickers[index] === ticker);
  const singleAapl = Array.isArray(requestedTickers)
    && requestedTickers.length === 1
    && requestedTickers[0] === 'AAPL';
  if (!exactDefault && !singleAapl) {
    throw new Error('[E2E fixture] Alerts support AAPL or the default AAPL, MSFT, NVDA list.');
  }
  const supportedTickers: readonly string[] = singleAapl ? ['AAPL'] : FIXTURE_SYMBOLS;
  const setups = [
    { ticker: 'AAPL', name: 'Apple', price: 231.42, change: 1.28, score: 92, setup: 'Support reclaimed with improving participation.' },
    { ticker: 'MSFT', name: 'Microsoft', price: 518.17, change: 0.64, score: 86, setup: 'Compression is resolving above the value area.' },
    { ticker: 'NVDA', name: 'NVIDIA', price: 181.06, change: -0.31, score: 79, setup: 'Momentum cooled while the primary trend held.' },
  ];
  return {
    rows: setups.filter((item) => supportedTickers.includes(item.ticker)).map((item, index) => ({
      ticker: item.ticker,
      rank: index + 1,
      lane: index === 0 ? 'Priority' : 'Review',
      name: item.name,
      sector: 'Technology',
      industry: 'Deterministic fixture',
      price: item.price,
      change_percent: item.change,
      annual_volatility: 24 + index * 4,
      scanner_score: item.score - 3,
      score: item.score,
      setup: item.setup,
      provider: FIXTURE_PROVIDER,
      provider_note: 'E2E-only metadata; no market claim.',
      trend_50d: item.ticker === 'AAPL' ? 224.18 : null,
      distance_from_52w_high: item.ticker === 'AAPL' ? -0.034 : null,
      distance_from_52w_low: item.ticker === 'AAPL' ? 0.392 : null,
      summary: item.ticker === 'AAPL' ? {
        business_summary: 'Apple designs consumer devices, software, and services for customers worldwide.',
        country: 'United States',
        website: 'https://www.apple.com',
        employees: 164000,
        market_cap: '$3.46T',
        trailing_pe: 35.2,
        forward_pe: 31.4,
        revenue_growth: 0.052,
        profit_margins: 0.241,
        return_on_equity: 1.71,
        recommendation: 'buy',
        target_mean_price: 245.75,
        analyst_count: 42,
        beta: 1.18,
        fifty_two_week_high: 239.98,
        fifty_two_week_low: 164.08,
      } : {},
      ridge: item.ticker === 'AAPL' ? { state: 'constructive', recommendation: 'Trend confirmed', trend_confirmed: true } : {},
      flow: item.ticker === 'AAPL' ? { state: 'improving', score: 78, signal: 'Accumulation' } : {},
      auction: item.ticker === 'AAPL' ? { location: 'Above value', poc: 227, vah: 230, val: 224, distance_to_poc: 0.0195 } : {},
    })),
    alerts: setups.filter((item) => supportedTickers.includes(item.ticker)).map((item, index) => ({
      id: `${item.ticker.toLowerCase()}-fixture`,
      ticker: item.ticker,
      rank: index + 1,
      lane: index === 0 ? 'Priority' : 'Review',
      score: item.score,
      severity: index === 0 ? 'High' : 'Info',
      category: 'Fixture',
      title: `${item.ticker} deterministic setup`,
      message: item.setup,
      action: 'Open the Ticker Lens.',
    })),
    digest: {
      generated_at: '2026-08-19T12:00:00.000Z',
      headline: 'Deterministic Pulse ready',
      summary: 'Three fixed symbols are available for cloud simulator proof.',
      severity_counts: { High: 1, Info: 2 },
      category_counts: { Fixture: 3 },
      lane_counts: { Priority: 1, Review: 2 },
      priority_tickers: ['AAPL'],
      risk_tickers: [],
      flow_shift_tickers: ['MSFT'],
      next_steps: ['Open AAPL Lens'],
    },
    provider: FIXTURE_PROVIDER,
    provider_note: 'Fixture mode is visible in the app.',
    errors: [],
    meta: { provider: FIXTURE_PROVIDER, errors: [], fixture: true },
    watchlist: null,
    tickers: [...supportedTickers],
  };
}

const priceDates = ['2026-08-13', '2026-08-14', '2026-08-17', '2026-08-18', '2026-08-19'];
const line = (values: readonly number[]) => priceDates.map((date, index) => ({ date, value: values[index] }));

function torquePayload() {
  return {
    chart_type: 'torque',
    ticker: 'AAPL',
    provider: FIXTURE_PROVIDER,
    period: '5d',
    meta: { provider: FIXTURE_PROVIDER, fixture: true },
    levels: {},
    series: {
      price: {
        close: line([225, 227, 226, 229, 231]),
        ema75: line([224, 225, 226, 227, 228]),
        sma50: line([222, 223, 224, 225, 226]),
        sma200: line([210, 211, 212, 213, 214]),
      },
      fundamentals: {
        revenue: [{ label: 'FY25', value: 416.2 }, { label: 'FY26E', value: 432.8 }],
        gross_margin: [{ label: 'FY25', value: 46.2 }, { label: 'FY26E', value: 46.8 }],
        operating_margin: [{ label: 'FY25', value: 31.5 }, { label: 'FY26E', value: 32.1 }],
      },
    },
    rows: [],
    torque: { posture: 'constructive', fixture: true },
  };
}

function auctionPayload(period: string) {
  const closes = [225, 227, 226, 229, 231];
  return {
    datasets: [{
      chart_type: 'auction',
      ticker: 'AAPL',
      period,
      meta: { provider: FIXTURE_PROVIDER },
      levels: { vah: 230, val: 224, poc: 227 },
      series: {
        ohlcv: priceDates.map((date, index) => ({
          date,
          open: closes[index] - 1,
          high: closes[index] + 2,
          low: closes[index] - 2,
          close: closes[index],
          volume: 50_000_000 + index * 1_000_000,
        })),
      },
      rows: [],
    }],
    provider: FIXTURE_PROVIDER,
    provider_note: 'E2E-only chart.',
    meta: { provider: FIXTURE_PROVIDER, errors: [], fixture: true },
  };
}

function moneylinePayload() {
  return {
    chart_type: 'moneyline',
    ticker: 'AAPL',
    meta: { current_price: 231.42, provider: FIXTURE_PROVIDER, fixture: true },
    rows: [220, 225, 230, 235, 240].map((strike, index) => ({
      strike,
      call_open_interest: 1800 - index * 190,
      put_open_interest: 700 + index * 210,
      call_last: 13 - index * 2,
      put_last: 2 + index * 1.5,
      net_open_interest: 1100 - index * 400,
      put_call_ratio: (700 + index * 210) / (1800 - index * 190),
    })),
  };
}

function ndjson(events: readonly JsonObject[]): string {
  return `${events.map((event) => JSON.stringify(event)).join('\n')}\n`;
}

function researchResponse(state: FixtureState, signal: AbortSignal | null): Response {
  state.researchAttempts += 1;
  if (state.researchAttempts === 1) {
    return streamResponse(ndjson([
      { type: 'start', model: FIXTURE_MODEL, tools: [...MOBILE_AGENT_TOOLS] },
      { type: 'text', text: 'Deterministic partial evidence: AAPL held its five-day value area. ' },
    ]), signal, true);
  }

  const summary = 'AAPL remains above its deterministic support band. Momentum and options positioning agree, while this fixture makes no live-market claim.';
  return streamResponse(ndjson([
    { type: 'start', model: FIXTURE_MODEL, tools: [...MOBILE_AGENT_TOOLS] },
    { type: 'tool_call', id: 'fixture-packet', name: 'ticker_research_bundle', input: { ticker: 'AAPL' } },
    { type: 'tool_result', id: 'fixture-packet', name: 'ticker_research_bundle', ok: true, duration_ms: 42, result: { ticker: 'AAPL', periods: ['1mo', '3mo', '1y'] }, artifacts: [] },
    { type: 'tool_call', id: 'fixture-analysis', name: 'analyze_ticker', input: { ticker: 'AAPL', period: '1y' } },
    { type: 'tool_result', id: 'fixture-analysis', name: 'analyze_ticker', ok: true, duration_ms: 24, result: { posture: 'constructive' }, artifacts: [] },
    { type: 'tool_call', id: 'fixture-sources', name: 'sec_source_pack', input: { ticker: 'AAPL' } },
    { type: 'tool_result', id: 'fixture-sources', name: 'sec_source_pack', ok: true, duration_ms: 18, result: { filings: 2 }, artifacts: [{ type: 'source-pack', ticker: 'AAPL', provider: FIXTURE_PROVIDER }] },
    { type: 'text', text: summary },
    { type: 'done', stop_reason: 'end_turn', text: summary, tool_trace: ['ticker_research_bundle', 'analyze_ticker', 'sec_source_pack'] },
  ]), signal, false);
}

function createHandler(state: FixtureState): FetchLike {
  return async (input, init = {}) => {
    const url = requestUrl(input);
    const method = requestMethod(input, init);
    const route = url.pathname;
    if (init.signal?.aborted) throw abortError();

    if (method === 'GET' && route === API_ENDPOINTS.health) {
      return jsonResponse({ ok: true, service: 'undercurrent-e2e-fixture' });
    }
    if (method === 'GET' && route === API_ENDPOINTS.tools) return jsonResponse(toolCatalog());
    if (method === 'GET' && route === API_ENDPOINTS.search) return jsonResponse(searchPayload(url));
    if (method === 'POST' && route === API_ENDPOINTS.alerts) return jsonResponse(pulsePayload(jsonBody(init)));
    if (method === 'POST' && route === API_ENDPOINTS.torque) {
      expectAapl(jsonBody(init));
      return jsonResponse(torquePayload());
    }
    if (method === 'POST' && route === API_ENDPOINTS.auction) {
      const body = jsonBody(init);
      expectAapl(body);
      const period = typeof body.period === 'string' ? body.period : '';
      if (!(LENS_AUCTION_PERIODS as readonly string[]).includes(period)) {
        throw new Error('[E2E fixture] AAPL Auction expects a supported Lens chart period.');
      }
      return jsonResponse(auctionPayload(period));
    }
    if (method === 'POST' && route === API_ENDPOINTS.moneyline) {
      expectAapl(jsonBody(init));
      return jsonResponse(moneylinePayload());
    }
    if (method === 'POST' && route === API_ENDPOINTS.agentStream) {
      expectExactResearchBoundary(jsonBody(init));
      return researchResponse(state, init.signal ?? null);
    }
    if (method === 'POST' && route === API_ENDPOINTS.agentChat) {
      expectExactResearchBoundary(jsonBody(init));
      return jsonResponse({
        ok: true,
        model: FIXTURE_MODEL,
        tools: [...MOBILE_AGENT_TOOLS],
        text: 'AAPL deterministic fallback completed.',
        stop_reason: 'end_turn',
        tool_calls: [{ name: 'ticker_research_bundle', ok: true, duration_ms: 42, error: null }],
        tool_trace: ['ticker_research_bundle'],
        artifacts: [],
        articles: [],
      });
    }
    throw new Error(`[E2E fixture] Unexpected request: ${method} ${route}`);
  };
}

export function createE2EFixtureFetch(): FetchLike {
  return createHandler({ researchAttempts: 0 });
}

const defaultState: FixtureState = { researchAttempts: 0 };
export const e2eFetch: FetchLike = createHandler(defaultState);

export function resetE2EFixtureStateForTests(): void {
  defaultState.researchAttempts = 0;
}
