import {
  ApiClient,
  ApiError,
  RequestCoordinator,
  TIMEOUT_MS,
} from '@/src/api/client';
import { MOBILE_AGENT_TOOLS } from '@/src/api/agentTools';
import { API_ENDPOINTS, buildApiConfig, encodeSymbol, normalizeSymbol } from '@/src/api/endpoints';

jest.mock('expo/fetch', () => ({ fetch: jest.fn() }));

type MockResponseInit = {
  status?: number;
  contentType?: string;
  body?: unknown;
  text?: string;
};

function response({ status = 200, contentType = 'application/json', body, text }: MockResponseInit) {
  const raw = text ?? JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name: string) => (name.toLowerCase() === 'content-type' ? contentType : null) },
    json: async () => JSON.parse(raw),
    text: async () => raw,
    body: null,
  } as unknown as Response;
}

function streamedResponse(
  chunks: Uint8Array[],
  { status = 200, contentType = 'application/x-ndjson' } = {},
) {
  let index = 0;
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => contentType },
    text: async () => '',
    body: {
      getReader: () => ({
        read: async () =>
          index < chunks.length ? { done: false, value: chunks[index++] } : { done: true, value: undefined },
        cancel: jest.fn(async () => undefined),
        releaseLock: jest.fn(),
      }),
    },
  } as unknown as Response;
}

describe('endpoint and configuration safety', () => {
  it('uses the deployed HTTPS backend by default, permits a public HTTPS override, and trims slashes', () => {
    expect(buildApiConfig(undefined)).toEqual({
      status: 'configured',
      baseUrl: 'https://underlying-terminal-production.up.railway.app',
    });
    expect(buildApiConfig('https://example.test///')).toEqual({
      status: 'configured',
      baseUrl: 'https://example.test',
    });
    expect(buildApiConfig('http://example.test')).toMatchObject({ status: 'invalid', baseUrl: null });
    expect(buildApiConfig('   ', { defaultBaseUrl: null })).toMatchObject({ status: 'missing', baseUrl: null });
  });

  it('defines the exact backend routes', () => {
    expect(API_ENDPOINTS).toMatchObject({
      health: '/api/health',
      tools: '/api/agent/tools',
      search: '/api/data/search',
      resolveWatchlist: '/api/watchlists/resolve',
      alerts: '/api/watchlists/alerts',
      auction: '/api/data/charts/auction',
      torque: '/api/data/tools/torque',
      moneyline: '/api/data/tools/moneyline',
      agentChat: '/api/agent/chat',
      agentStream: '/api/agent/chat/stream',
    });
  });

  it('uppercases and validates symbols before URI encoding', () => {
    expect(normalizeSymbol(' brk.b ')).toBe('BRK.B');
    expect(normalizeSymbol(' ^gspc ')).toBe('^GSPC');
    expect(encodeSymbol('brk.b')).toBe('BRK.B');
    expect(encodeSymbol('^gspc')).toBe('%5EGSPC');
    expect(encodeSymbol('rds-a')).toBe('RDS-A');
    expect(() => encodeSymbol('A B')).toThrow(/symbol/i);
    expect(() => normalizeSymbol('A/B')).toThrow(/symbol/i);
    expect(() => normalizeSymbol('A?B')).toThrow(/symbol/i);
    expect(() => normalizeSymbol('G^SPC')).toThrow(/symbol/i);
    expect(() => normalizeSymbol('^^GSPC')).toThrow(/symbol/i);
    expect(() => normalizeSymbol('^')).toThrow(/symbol/i);
    expect(() => normalizeSymbol('A'.repeat(16))).toThrow(/symbol/i);
  });
});

describe('ApiClient', () => {
  it('uses exact timeout classes', () => {
    expect(TIMEOUT_MS).toEqual({ normal: 30_000, capability: 10_000, search: 15_000, researchIdle: 45_000 });
  });

  it('searches with encoded query parameters and filters hostile or partial provider results', async () => {
    const fetchImpl = jest.fn(async () =>
      response({
        body: {
          query: 'S&P 500',
          results: [
            { symbol: '^GSPC', name: 'S&P 500', exchange: 'SNP', asset_type: 'index' },
            { symbol: 'btc-usd', name: 'Bitcoin USD', exchange: 'CCC', asset_type: 'crypto' },
            { symbol: 'A'.repeat(16), name: 'Too long for Lens', exchange: 'TEST', asset_type: 'equity' },
            { symbol: '</script>', name: 'Hostile', exchange: 'Nowhere', asset_type: 'equity' },
            { symbol: 'AAPL', name: 'Apple Inc.', asset_type: 'equity' },
            { symbol: 'ES=F', name: 'E-mini S&P 500', exchange: 'CME', asset_type: 'future' },
            'not-an-object',
          ],
          provider: 'Yahoo Finance via yfinance',
        },
      }),
    );
    const client = new ApiClient({ baseUrl: 'https://api.test', fetchImpl });

    await expect(client.searchSecurities({ query: ' S&P 500 ', limit: 3 })).resolves.toEqual({
      query: 'S&P 500',
      results: [
        { symbol: '^GSPC', name: 'S&P 500', exchange: 'SNP', assetType: 'index' },
        { symbol: 'BTC-USD', name: 'Bitcoin USD', exchange: 'CCC', assetType: 'crypto' },
      ],
      provider: 'Yahoo Finance via yfinance',
    });
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('https://api.test/api/data/search?q=S%26P+500&limit=3');
    expect(init).toMatchObject({ method: 'GET' });
    expect(init.signal).toBeInstanceOf(AbortSignal);
  });

  it('rejects invalid security searches locally without fetching', async () => {
    const fetchImpl = jest.fn();
    const client = new ApiClient({ baseUrl: 'https://api.test', fetchImpl });
    const invalidRequests = [
      { query: '   ' },
      { query: 'x'.repeat(101) },
      { query: 'Apple', limit: 0 },
      { query: 'Apple', limit: 11 },
      { query: 'Apple', limit: 1.5 },
    ];

    for (const request of invalidRequests) {
      await expect(client.searchSecurities(request)).rejects.toMatchObject({ kind: 'validation' });
    }
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it('supports local cancellation for security search', async () => {
    let requestSignal: AbortSignal | undefined;
    const fetchImpl = jest.fn((_url: RequestInfo | URL, init?: RequestInit) => {
      requestSignal = init?.signal ?? undefined;
      return new Promise<Response>((_resolve, reject) => {
        requestSignal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
      });
    });
    const controller = new AbortController();
    const client = new ApiClient({ baseUrl: 'https://api.test', fetchImpl });
    const request = client.searchSecurities({ query: 'Apple' }, { signal: controller.signal });

    controller.abort();

    await expect(request).rejects.toMatchObject({ kind: 'cancelled' });
    expect(requestSignal?.aborted).toBe(true);
  });

  it('normalizes partial alert successes instead of throwing', async () => {
    const fetchImpl = jest.fn(async () =>
      response({
        body: {
          rows: [{
            ticker: 'aapl',
            rank: 1,
            lane: 'Priority',
            trend_50d: 0.04,
            distance_from_52w_high: -0.08,
            distance_from_52w_low: 0.31,
            summary: {
              business_summary: 'Builds consumer technology.',
              country: 'United States',
              website: 'https://apple.com',
              employees: 164_000,
              market_cap: '3.42T',
              trailing_pe: 'N/A',
              forward_pe: 28.4,
              price_to_sales: 8.6,
              price_to_book: 'N/A',
              revenue_growth: 0.052,
              profit_margins: 0.244,
              return_on_equity: 1.51,
              debt_to_equity: 145.2,
              recommendation: 'buy',
              target_mean_price: 245.5,
              analyst_count: 42,
              beta: 'N/A',
              fifty_two_week_high: 237.49,
              fifty_two_week_low: 164.08,
            },
          }],
          alerts: [{ id: 'aapl-priority', ticker: 'aapl', severity: 'High', category: 'Setup', title: 'Priority setup', message: 'Ready', action: 'Review' }],
          digest: { headline: 'One available', summary: 'AAPL is ready', generated_at: '2026-08-19T00:00:00Z', severity_counts: { High: 1 }, category_counts: { Setup: 1 }, lane_counts: { Priority: 1 }, priority_tickers: ['aapl'], risk_tickers: [], flow_shift_tickers: [], next_steps: ['Review'] },
          provider: 'fake',
          meta: { errors: [{ ticker: 'msft', error: 'unavailable' }], result_count: 1, error_count: 1 },
          watchlist: { name: 'Full', source_url: 'https://www.tradingview.com/watchlists/1/', tickers: ['aapl', 'msft'] },
          tickers: ['aapl'],
        },
      }),
    );
    const client = new ApiClient({ baseUrl: 'https://api.test', fetchImpl });
    const result = await client.watchlistAlerts({ tickers: ['aapl', 'msft'] });
    expect(result.status).toBe('partial');
    expect(result.rows[0].ticker).toBe('AAPL');
    expect(result.rows[0]).toMatchObject({
      lane: 'Priority',
      rank: 1,
      trend50d: 0.04,
      distanceFrom52WeekHigh: -0.08,
      distanceFrom52WeekLow: 0.31,
      fundamentals: {
        businessSummary: 'Builds consumer technology.',
        country: 'United States',
        website: 'https://apple.com',
        employees: 164_000,
        marketCap: '3.42T',
        trailingPe: null,
        forwardPe: 28.4,
        priceToSales: 8.6,
        priceToBook: null,
        revenueGrowth: 0.052,
        profitMargins: 0.244,
        returnOnEquity: 1.51,
        debtToEquity: 145.2,
        recommendation: 'buy',
        targetMeanPrice: 245.5,
        analystCount: 42,
        beta: null,
        fiftyTwoWeekHigh: 237.49,
        fiftyTwoWeekLow: 164.08,
      },
    });
    expect(result.alerts[0]).toMatchObject({ ticker: 'AAPL', severity: 'High', title: 'Priority setup' });
    expect(result.digest).toMatchObject({ headline: 'One available', priorityTickers: ['AAPL'] });
    expect(result.errors).toEqual([{ ticker: 'MSFT', error: 'unavailable' }]);
    expect(result.watchlist?.tickers).toEqual(['AAPL', 'MSFT']);
  });

  it('keeps valid resolved-watchlist symbols but rejects an all-invalid preview', async () => {
    const fetchImpl = jest
      .fn()
      .mockResolvedValueOnce(response({
        body: {
          watchlist: {
            id: 123,
            name: 'Mixed',
            source_url: 'https://www.tradingview.com/watchlists/123/',
            tickers: ['aapl', 'BAD/SYMBOL', 'msft'],
          },
          tickers: ['aapl', 'BAD/SYMBOL', 'msft'],
          max_results: 10,
        },
      }))
      .mockResolvedValueOnce(response({
        body: {
          watchlist: {
            id: 123,
            name: 'Invalid',
            source_url: 'https://www.tradingview.com/watchlists/123/',
            tickers: ['BAD/SYMBOL'],
          },
          tickers: ['BAD/SYMBOL'],
          max_results: 10,
        },
      }));
    const client = new ApiClient({ baseUrl: 'https://api.test', fetchImpl });

    await expect(client.resolveWatchlist({ watchlistUrl: 'https://www.tradingview.com/watchlists/123/' }))
      .resolves.toMatchObject({ tickers: ['AAPL', 'MSFT'] });
    await expect(client.resolveWatchlist({ watchlistUrl: 'https://www.tradingview.com/watchlists/123/' }))
      .rejects.toMatchObject({ kind: 'protocol', message: expect.stringMatching(/valid tickers/i) });
  });

  it('normalizes one moneyline row source when rows are repeated', async () => {
    const rows = [{ strike: 100, call_open_interest: 2, put_open_interest: 1 }];
    const fetchImpl = jest.fn(async () =>
      response({ body: { chart_type: 'moneyline', ticker: 'aapl', rows, series: { strikes: rows }, meta: { rows } } }),
    );
    const client = new ApiClient({ baseUrl: 'https://api.test', fetchImpl });
    const result = await client.moneyline({ ticker: 'aapl' });
    expect(result.ticker).toBe('AAPL');
    expect(result.rows).toHaveLength(1);
    expect(result.rows[0]).toMatchObject({ strike: 100, callOpenInterest: 2, putOpenInterest: 1 });
    expect(result).not.toHaveProperty('series');
  });

  it('turns HTML and JSON failures into typed ApiError values', async () => {
    const htmlClient = new ApiClient({
      baseUrl: 'https://api.test',
      fetchImpl: jest.fn(async () => response({ status: 502, contentType: 'text/html', text: '<h1>Bad gateway</h1>' })),
    });
    await expect(htmlClient.health()).rejects.toMatchObject({
      name: 'ApiError',
      kind: 'http',
      status: 502,
      message: 'The service returned a non-JSON error (HTTP 502).',
    });

    const jsonClient = new ApiClient({
      baseUrl: 'https://api.test',
      fetchImpl: jest.fn(async () => response({ status: 400, body: { error: 'bad ticker' } })),
    });
    await expect(jsonClient.health()).rejects.toMatchObject({ kind: 'http', message: 'bad ticker' });
  });

  it('treats HTTP 200 {error} from non-stream agent chat as failure', async () => {
    const client = new ApiClient({
      baseUrl: 'https://api.test',
      fetchImpl: jest.fn(async () => response({ body: { error: 'agent offline' } })),
    });
    await expect(
      client.agentChat({ messages: [{ role: 'user', content: 'status?' }] }),
    ).rejects.toMatchObject({ kind: 'api', message: 'agent offline' });
  });

  it('always sends the exact six tools and bounds agent input', async () => {
    const fetchImpl = jest.fn(async () => response({ body: { ok: true, model: 'test', tools: MOBILE_AGENT_TOOLS, text: 'ok', tool_calls: [], tool_trace: [], artifacts: [], articles: [], stop_reason: 'end_turn' } }));
    const client = new ApiClient({ baseUrl: 'https://api.test', fetchImpl });
    await client.agentChat({
      messages: Array.from({ length: 45 }, (_, index) => ({ role: index % 2 ? 'assistant' as const : 'user' as const, content: 'x'.repeat(13_000) })),
      context: 'c'.repeat(3_000),
    });
    const [, init] = fetchImpl.mock.calls[0] as unknown as [RequestInfo | URL, RequestInit];
    const body = JSON.parse(String(init?.body));
    expect(body.tools).toEqual(MOBILE_AGENT_TOOLS);
    expect(body.tool_policy).toBe('exact');
    expect(body.messages).toHaveLength(40);
    expect(body.messages.every((message: { content: string }) => message.content.length <= 12_000)).toBe(true);
    expect(body.context).toHaveLength(2_000);
  });

  it('rejects a non-stream response that does not echo the exact mobile tool boundary', async () => {
    const fetchImpl = jest.fn(async () => response({
      body: {
        ok: true,
        model: 'test',
        tools: MOBILE_AGENT_TOOLS.slice(0, -1),
        text: 'unsafe',
        tool_calls: [],
        tool_trace: [],
        artifacts: [],
        articles: [],
        stop_reason: 'end_turn',
      },
    }));
    const client = new ApiClient({ baseUrl: 'https://api.test', fetchImpl });
    await expect(client.agentChat({ messages: [{ role: 'user', content: 'go' }] })).rejects.toMatchObject({
      kind: 'protocol',
      message: expect.stringMatching(/tool allowlist mismatch/i),
    });
  });

  it('guards capability preview fields used to approve the mobile tool set', async () => {
    const fetchImpl = jest.fn(async () =>
      response({
        body: {
          agent_ready: true,
          model: 'test',
          tool_count: 1,
          tools: [{
            name: 'analyze_ticker',
            title: 'Analyze ticker',
            group: 'research',
            summary: 'Build a bounded ticker analysis.',
            when_to_use: 'Use for one ticker.',
            returns: 'Analysis payload.',
            cost: 'fast',
            produces_images: false,
            agent: true,
            mcp: true,
            http: { method: 'POST', path: '/api/analyze' },
            arguments: ['ticker'],
            required: ['ticker'],
          }],
        },
      }),
    );
    const client = new ApiClient({ baseUrl: 'https://api.test', fetchImpl });
    const catalog = await client.tools();
    expect(catalog.tools[0]).toEqual({
      name: 'analyze_ticker',
      title: 'Analyze ticker',
      group: 'research',
      summary: 'Build a bounded ticker analysis.',
      whenToUse: 'Use for one ticker.',
      returns: 'Analysis payload.',
      cost: 'fast',
      producesImages: false,
      agent: true,
      mcp: true,
      http: { method: 'POST', path: '/api/analyze' },
      arguments: ['ticker'],
      required: ['ticker'],
    });
  });

  it('exposes abort as a typed local cancellation', async () => {
    const fetchImpl = jest.fn((_url: RequestInfo | URL, init?: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
      }),
    );
    const controller = new AbortController();
    const client = new ApiClient({ baseUrl: 'https://api.test', fetchImpl });
    const request = client.health({ signal: controller.signal });
    controller.abort();
    await expect(request).rejects.toMatchObject({ kind: 'cancelled' });
  });

  it('classifies research idle abort as timeout, not local cancellation', async () => {
    jest.useFakeTimers();
    try {
      const fetchImpl = jest.fn((_url: RequestInfo | URL, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
        }),
      );
      const client = new ApiClient({ baseUrl: 'https://api.test', fetchImpl });
      const session = client.agentStream({ messages: [{ role: 'user', content: 'go' }] });
      const result = expect(session.result).rejects.toMatchObject({ kind: 'timeout' });
      await jest.advanceTimersByTimeAsync(TIMEOUT_MS.researchIdle);
      await result;
    } finally {
      jest.useRealTimers();
    }
  });

  it('streams fragmented records through one controller and completes once', async () => {
    const encoder = new TextEncoder();
    const payload = `${'\n'.repeat(4096)}${JSON.stringify({ type: 'start', model: 'test', tools: [...MOBILE_AGENT_TOOLS].reverse() })}\r\n${JSON.stringify({ type: 'text', text: 'café 📈' })}\n${JSON.stringify({ type: 'done', text: 'café 📈', stop_reason: 'end_turn', tool_trace: [] })}`;
    const bytes = encoder.encode(payload);
    const fetchImpl = jest.fn(async () =>
      streamedResponse(Array.from(bytes, (byte) => Uint8Array.of(byte))),
    );
    const events: unknown[] = [];
    const client = new ApiClient({ baseUrl: 'https://api.test', fetchImpl });
    const session = client.agentStream({ messages: [{ role: 'user', content: 'go' }] }, { onEvent: (event) => events.push(event) });
    const result = await session.result;
    expect(result).toMatchObject({ transport: 'stream', state: { status: 'completed', text: 'café 📈' } });
    expect(events).toHaveLength(3);
    const firstInit = (fetchImpl.mock.calls[0] as unknown as [RequestInfo | URL, RequestInit])[1];
    expect(firstInit.signal).toBe(session.controller.signal);
  });

  it.each([404, 405, 501])('falls back only for a pre-body %s and reuses the controller', async (status) => {
    const fetchImpl = jest
      .fn()
      .mockResolvedValueOnce(streamedResponse([], { status, contentType: 'application/json' }))
      .mockResolvedValueOnce(response({ body: { ok: true, model: 'test', tools: MOBILE_AGENT_TOOLS, text: 'fallback', stop_reason: 'end_turn', tool_calls: [], tool_trace: [], artifacts: [], articles: [] } }));
    const client = new ApiClient({ baseUrl: 'https://api.test', fetchImpl });
    const session = client.agentStream({ messages: [{ role: 'user', content: 'go' }] });
    await expect(session.result).resolves.toMatchObject({ transport: 'fallback', fallback: { text: 'fallback' } });
    expect(fetchImpl).toHaveBeenCalledTimes(2);
    expect(fetchImpl.mock.calls[0][1]?.signal).toBe(session.controller.signal);
    expect(fetchImpl.mock.calls[1][1]?.signal).toBe(session.controller.signal);
  });

  it('starts a fresh idle timeout when the non-stream fallback begins', async () => {
    jest.useFakeTimers();
    try {
      let resolveStream!: (value: Response) => void;
      let fallbackSignal: AbortSignal | undefined;
      const fetchImpl = jest.fn((_url: RequestInfo | URL, init?: RequestInit) => {
        if (fetchImpl.mock.calls.length === 1) {
          return new Promise<Response>((resolve) => {
            resolveStream = resolve;
          });
        }
        fallbackSignal = init?.signal ?? undefined;
        return new Promise<Response>((_resolve, reject) => {
          fallbackSignal?.addEventListener(
            'abort',
            () => reject(new DOMException('Aborted', 'AbortError')),
            { once: true },
          );
        });
      });
      const client = new ApiClient({ baseUrl: 'https://api.test', fetchImpl });
      const session = client.agentStream({ messages: [{ role: 'user', content: 'go' }] });
      const result = expect(session.result).rejects.toMatchObject({ kind: 'timeout' });

      await jest.advanceTimersByTimeAsync(5_000);
      resolveStream(streamedResponse([], { status: 404, contentType: 'application/json' }));
      await Promise.resolve();
      expect(fetchImpl).toHaveBeenCalledTimes(2);

      await jest.advanceTimersByTimeAsync(TIMEOUT_MS.researchIdle - 5_000);
      expect(fallbackSignal?.aborted).toBe(false);

      await jest.advanceTimersByTimeAsync(5_000);
      await result;
    } finally {
      jest.useRealTimers();
    }
  });

  it('does not fall back after bytes arrive or for other HTTP failures', async () => {
    const encoder = new TextEncoder();
    const interruptedFetch = jest.fn(async () =>
      streamedResponse([
        encoder.encode(`${JSON.stringify({ type: 'start', model: 'test', tools: MOBILE_AGENT_TOOLS })}\n`),
        encoder.encode('{"type":"text","text":"partial'),
      ]),
    );
    const interrupted = new ApiClient({
      baseUrl: 'https://api.test',
      fetchImpl: interruptedFetch,
    });
    await expect(interrupted.agentStream({ messages: [{ role: 'user', content: 'go' }] }).result).rejects.toMatchObject({
      kind: 'protocol',
    });
    expect(interruptedFetch).toHaveBeenCalledTimes(1);

    const failedFetch = jest.fn(async () => streamedResponse([], { status: 500, contentType: 'text/html' }));
    const failed = new ApiClient({ baseUrl: 'https://api.test', fetchImpl: failedFetch });
    await expect(failed.agentStream({ messages: [{ role: 'user', content: 'go' }] }).result).rejects.toMatchObject({
      kind: 'http',
      status: 500,
    });
    expect(failedFetch).toHaveBeenCalledTimes(1);
  });

  it('cancels the response reader without masking an NDJSON protocol failure', async () => {
    const cancel = jest.fn(async () => {
      throw new Error('reader cancellation failed');
    });
    const releaseLock = jest.fn();
    const read = jest.fn(async () => ({
      done: false as const,
      value: new TextEncoder().encode('not-json\n'),
    }));
    const fetchImpl = jest.fn(async () => ({
      ok: true,
      status: 200,
      headers: { get: () => 'application/x-ndjson' },
      body: { getReader: () => ({ read, cancel, releaseLock }) },
    }) as unknown as Response);
    const client = new ApiClient({ baseUrl: 'https://api.test', fetchImpl });

    await expect(
      client.agentStream({ messages: [{ role: 'user', content: 'go' }] }).result,
    ).rejects.toMatchObject({ kind: 'protocol' });
    expect(cancel).toHaveBeenCalledTimes(1);
    expect(cancel).toHaveBeenCalledWith(expect.any(Error));
    expect(releaseLock).toHaveBeenCalledTimes(1);
  });

  it('cancels locally mid-record and never emits the incomplete record', async () => {
    const encoder = new TextEncoder();
    let release!: () => void;
    let reads = 0;
    const fetchImpl = jest.fn(async () => ({
      ...streamedResponse([]),
      body: {
        getReader: () => ({
          read: async () => {
            reads += 1;
            if (reads === 1) return { done: false, value: encoder.encode('{"type":"text"') };
            await new Promise<void>((resolve) => (release = resolve));
            throw new DOMException('Aborted', 'AbortError');
          },
          releaseLock: jest.fn(),
        }),
      },
    }) as unknown as Response);
    const events: unknown[] = [];
    const client = new ApiClient({ baseUrl: 'https://api.test', fetchImpl });
    const session = client.agentStream({ messages: [{ role: 'user', content: 'go' }] }, { onEvent: (event) => events.push(event) });
    await Promise.resolve();
    await Promise.resolve();
    session.cancel();
    release?.();
    await expect(session.result).rejects.toMatchObject({ kind: 'cancelled' });
    expect(events).toEqual([]);
  });
});

describe('RequestCoordinator', () => {
  it('prevents a late replaced request from overwriting newer state', async () => {
    const coordinator = new RequestCoordinator<number>();
    let resolveOld!: (value: number) => void;
    const old = coordinator.run(() => new Promise<number>((resolve) => (resolveOld = resolve)));
    const newer = coordinator.run(async () => 2);
    await expect(newer).resolves.toMatchObject({ accepted: true, value: 2 });
    resolveOld(1);
    await expect(old).resolves.toMatchObject({ accepted: false, value: 1 });
  });

  it('aborts the prior request when replacing it', async () => {
    const coordinator = new RequestCoordinator<number>();
    let firstSignal: AbortSignal | undefined;
    void coordinator.run(
      (signal) => {
        firstSignal = signal;
        return new Promise<number>(() => undefined);
      },
    );
    void coordinator.run(async () => 2);
    expect(firstSignal?.aborted).toBe(true);
  });
});

describe('ApiError', () => {
  it('is a stable typed error', () => {
    expect(new ApiError('validation', 'bad')).toMatchObject({ name: 'ApiError', kind: 'validation' });
  });
});
