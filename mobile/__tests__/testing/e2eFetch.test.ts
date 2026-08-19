import { API_ENDPOINTS } from '@/src/api/endpoints';
import { MOBILE_AGENT_TOOLS } from '@/src/api/agentTools';
import { e2eFetch, resetE2EFixtureStateForTests } from '@/src/testing/e2eFetch';

const API_ORIGIN = 'https://underlying-terminal-production.up.railway.app';

function request(path: string, init?: RequestInit) {
  return e2eFetch(`${API_ORIGIN}${path}`, init);
}

async function body(response: Response): Promise<Record<string, unknown>> {
  return await response.json() as Record<string, unknown>;
}

describe('deterministic E2E fixture transport', () => {
  beforeEach(() => resetE2EFixtureStateForTests());

  it('serves the exact six-tool capability and the default Pulse without external fetches', async () => {
    const tools = await request(API_ENDPOINTS.tools);
    expect(await body(tools)).toMatchObject({
      agent_ready: true,
      model: 'undercurrent-e2e-fixture',
      tool_count: MOBILE_AGENT_TOOLS.length,
      tools: MOBILE_AGENT_TOOLS.map((name) => expect.objectContaining({ name })),
    });

    const pulse = await request(API_ENDPOINTS.alerts, {
      method: 'POST',
      body: JSON.stringify({ tickers: ['AAPL', 'MSFT', 'NVDA'] }),
    });
    expect(await body(pulse)).toMatchObject({
      provider: 'Undercurrent deterministic fixture',
      tickers: ['AAPL', 'MSFT', 'NVDA'],
      rows: [
        expect.objectContaining({ ticker: 'AAPL', rank: 1 }),
        expect.objectContaining({ ticker: 'MSFT', rank: 2 }),
        expect.objectContaining({ ticker: 'NVDA', rank: 3 }),
      ],
    });
  });

  it('serves deterministic discovery and the lightweight AAPL overview', async () => {
    const search = await request(`${API_ENDPOINTS.search}?q=apple&limit=8`);
    expect(await body(search)).toMatchObject({
      query: 'apple',
      provider: 'Undercurrent deterministic fixture',
      results: [expect.objectContaining({ symbol: 'AAPL', name: 'Apple Inc.' })],
    });

    const overview = await request(API_ENDPOINTS.alerts, {
      method: 'POST',
      body: JSON.stringify({ ticker: 'AAPL' }),
    });
    expect(await body(overview)).toMatchObject({
      tickers: ['AAPL'],
      rows: [expect.objectContaining({
        ticker: 'AAPL',
        summary: expect.objectContaining({ market_cap: '$3.46T' }),
        ridge: expect.objectContaining({ state: 'constructive' }),
        flow: expect.objectContaining({ signal: 'Accumulation' }),
        auction: expect.objectContaining({ location: 'Above value' }),
      })],
    });
  });

  it('serves the exact AAPL Glance and Diagnose endpoints with chartable data', async () => {
    const torque = await request(API_ENDPOINTS.torque, {
      method: 'POST',
      body: JSON.stringify({ ticker: 'AAPL' }),
    });
    expect(await body(torque)).toMatchObject({
      chart_type: 'torque',
      ticker: 'AAPL',
      series: { price: { close: expect.any(Array) } },
    });

    const auction = await request(API_ENDPOINTS.auction, {
      method: 'POST',
      body: JSON.stringify({ ticker: 'AAPL', period: '5d' }),
    });
    expect(await body(auction)).toMatchObject({
      datasets: [expect.objectContaining({
        chart_type: 'auction',
        ticker: 'AAPL',
        period: '5d',
        series: { ohlcv: expect.any(Array) },
      })],
    });

    const visibleChart = await request(API_ENDPOINTS.auction, {
      method: 'POST',
      body: JSON.stringify({ ticker: 'AAPL', period: '3mo' }),
    });
    expect(await body(visibleChart)).toMatchObject({
      datasets: [expect.objectContaining({ ticker: 'AAPL', period: '3mo' })],
    });

    const moneyline = await request(API_ENDPOINTS.moneyline, {
      method: 'POST',
      body: JSON.stringify({ ticker: 'AAPL' }),
    });
    expect(await body(moneyline)).toMatchObject({
      chart_type: 'moneyline',
      ticker: 'AAPL',
      rows: expect.any(Array),
    });
  });

  it('keeps the first research stream cancellable and completes only the explicit retry', async () => {
    const controller = new AbortController();
    const init = {
      method: 'POST',
      body: JSON.stringify({
        messages: [{ role: 'user', content: 'Run bounded ticker research.' }],
        tools: [...MOBILE_AGENT_TOOLS],
        tool_policy: 'exact',
        context: 'Ticker: AAPL\nPeriod: 1y',
      }),
      signal: controller.signal,
    } satisfies RequestInit;

    const first = await request(API_ENDPOINTS.agentStream, init);
    const firstReader = first.body!.getReader();
    const partial = await firstReader.read();
    expect(new TextDecoder().decode(partial.value)).toContain('Deterministic partial evidence');
    const pending = firstReader.read();
    controller.abort();
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' });

    const retry = await request(API_ENDPOINTS.agentStream, { ...init, signal: new AbortController().signal });
    const retryReader = retry.body!.getReader();
    const completed = await retryReader.read();
    const text = new TextDecoder().decode(completed.value);
    expect(text).toContain('"type":"start"');
    expect(text).toContain('"type":"done"');
    expect(text).toContain('AAPL remains above its deterministic support band');
    await expect(retryReader.read()).resolves.toMatchObject({ done: true });
  });

  it('fails closed for an unexpected path, method, ticker, or research boundary', async () => {
    await expect(request('/api/unexpected')).rejects.toThrow(/Unexpected request: GET \/api\/unexpected/);
    await expect(request(API_ENDPOINTS.tools, { method: 'POST' })).rejects.toThrow(/Unexpected request: POST \/api\/agent\/tools/);
    await expect(request(API_ENDPOINTS.torque, {
      method: 'POST',
      body: JSON.stringify({ ticker: 'TSLA' }),
    })).rejects.toThrow(/AAPL/);
    await expect(request(API_ENDPOINTS.agentStream, {
      method: 'POST',
      body: JSON.stringify({
        messages: [{ role: 'user', content: 'go' }],
        tools: MOBILE_AGENT_TOOLS.slice(0, -1),
        tool_policy: 'exact',
        context: 'Ticker: AAPL\nPeriod: 1y',
      }),
    })).rejects.toThrow(/exact six-tool boundary/);
    await expect(request(API_ENDPOINTS.agentStream, {
      method: 'POST',
      body: JSON.stringify({
        messages: [{ role: 'user', content: 'go' }],
        tools: MOBILE_AGENT_TOOLS,
        context: 'Ticker: AAPL\nPeriod: 1y',
      }),
    })).rejects.toThrow(/exact tool policy/);
  });
});
