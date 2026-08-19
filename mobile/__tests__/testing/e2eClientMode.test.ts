import { fetch as expoFetch } from 'expo/fetch';

import { MOBILE_AGENT_TOOLS } from '@/src/api/agentTools';
import { ApiClient } from '@/src/api/client';
import { e2eFetch, resetE2EFixtureStateForTests } from '@/src/testing/e2eFetch';

jest.mock('expo/fetch', () => ({ fetch: jest.fn() }));

function liveHealthResponse(): Response {
  return {
    ok: true,
    status: 200,
    headers: { get: () => 'application/json' },
    body: null,
    json: async () => ({ ok: true, service: 'live-fetch' }),
    text: async () => '',
  } as unknown as Response;
}

describe('ApiClient E2E transport selection', () => {
  const liveFetch = expoFetch as jest.MockedFunction<typeof expoFetch>;

  beforeEach(() => {
    liveFetch.mockReset();
    resetE2EFixtureStateForTests();
  });

  it('keeps normal Expo Go and production clients on expo/fetch', async () => {
    liveFetch.mockResolvedValue(liveHealthResponse() as never);

    await expect(new ApiClient().health()).resolves.toEqual({ ok: true, service: 'live-fetch' });
    expect(liveFetch).toHaveBeenCalledTimes(1);
  });

  it('preserves explicit fetch injection', async () => {
    const injected = jest.fn(async () => liveHealthResponse());
    await expect(new ApiClient({ fetchImpl: injected }).health()).resolves.toEqual({ ok: true, service: 'live-fetch' });
    expect(injected).toHaveBeenCalledTimes(1);
  });

  it('runs fixture responses through every normalized client and stream boundary used by the proof', async () => {
    liveFetch.mockRejectedValue(new Error('live fetch must not run'));
    const client = new ApiClient({ fetchImpl: e2eFetch });

    await expect(client.watchlistAlerts({ tickers: ['AAPL', 'MSFT', 'NVDA'] })).resolves.toMatchObject({
      status: 'fresh',
      provider: 'Undercurrent deterministic fixture',
      rows: [
        expect.objectContaining({ ticker: 'AAPL', rank: 1 }),
        expect.objectContaining({ ticker: 'MSFT', rank: 2 }),
        expect.objectContaining({ ticker: 'NVDA', rank: 3 }),
      ],
    });
    await expect(client.torque({ ticker: 'AAPL' })).resolves.toMatchObject({
      chartType: 'torque',
      ticker: 'AAPL',
      raw: { provider: 'Undercurrent deterministic fixture' },
    });
    await expect(client.auction({ ticker: 'AAPL', period: '5d' })).resolves.toMatchObject({
      status: 'fresh',
      datasets: [expect.objectContaining({ chartType: 'auction', ticker: 'AAPL', period: '5d' })],
    });
    await expect(client.moneyline({ ticker: 'AAPL' })).resolves.toMatchObject({
      chartType: 'moneyline',
      ticker: 'AAPL',
      rows: expect.arrayContaining([expect.objectContaining({ strike: 220, callOpenInterest: 1800 })]),
    });

    const request = {
      messages: [{ role: 'user' as const, content: 'Run bounded ticker research.' }],
      context: 'Ticker: AAPL\nPeriod: 1y',
    };
    const streamedEvents: unknown[] = [];
    const first = client.agentStream(request, { onEvent: (event) => streamedEvents.push(event) });
    for (let index = 0; index < 5 && streamedEvents.length < 2; index += 1) {
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
    expect(streamedEvents).toEqual([
      expect.objectContaining({ type: 'start', model: 'undercurrent-e2e-fixture', tools: [...MOBILE_AGENT_TOOLS] }),
      expect.objectContaining({ type: 'text', text: expect.stringContaining('Deterministic partial evidence') }),
    ]);
    first.cancel();
    await expect(first.result).rejects.toMatchObject({ kind: 'cancelled' });

    const retry = client.agentStream(request);
    await expect(retry.result).resolves.toMatchObject({
      transport: 'stream',
      state: {
        status: 'completed',
        model: 'undercurrent-e2e-fixture',
        tools: [...MOBILE_AGENT_TOOLS],
        text: expect.stringContaining('AAPL remains above its deterministic support band'),
        error: null,
      },
    });
    expect(liveFetch).not.toHaveBeenCalled();
  });
});
