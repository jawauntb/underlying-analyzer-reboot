import { MOBILE_AGENT_TOOLS } from '@/src/api/agentTools';
import {
  buildResearchContext,
  buildResearchRequest,
  completionFromAgentResult,
  deriveResearchCapability,
  normalizeResearchRouteParams,
  RESEARCH_MESSAGE,
  RESEARCH_PERIODS,
} from '@/src/features/research/research-model';

const catalog = {
  agentReady: true,
  model: 'claude-sonnet',
  toolCount: 7,
  tools: [
    ...MOBILE_AGENT_TOOLS.map((name) => ({
      name,
      title: name,
      group: 'research',
      summary: '',
      whenToUse: '',
      returns: '',
      cost: 'low',
      producesImages: false,
      agent: true,
      mcp: false,
      http: { method: 'POST', path: `/api/${name}` },
      arguments: [],
      required: [],
    })),
    {
      name: 'compose_article',
      title: 'Compose article',
      group: 'content',
      summary: '',
      whenToUse: '',
      returns: '',
      cost: 'high',
      producesImages: false,
      agent: true,
      mcp: false,
      http: { method: 'POST', path: '/api/compose' },
      arguments: [],
      required: [],
    },
  ],
};

describe('research model', () => {
  it('normalizes the route without requesting data and defaults the period to 1y', () => {
    expect(normalizeResearchRouteParams({ symbol: ' aapl ' })).toEqual({
      ok: true,
      symbol: 'AAPL',
      period: '1y',
      recordId: null,
    });
    expect(normalizeResearchRouteParams({ symbol: 'MSFT', period: '10Y', recordId: 'run-1' })).toEqual({
      ok: true,
      symbol: 'MSFT',
      period: '10y',
      recordId: 'run-1',
    });
    expect(RESEARCH_PERIODS).toEqual(['5d', '1mo', '3mo', '6mo', '1y', '2y', '5y', '10y']);
  });

  it.each([
    [{}, /ticker/i],
    [{ symbol: 'AAPL/../../research' }, /ticker/i],
    [{ symbol: ['AAPL', 'MSFT'] }, /ticker/i],
    [{ symbol: 'AAPL', period: 'all' }, /period/i],
    [{ symbol: 'AAPL', recordId: '../run' }, /saved research/i],
  ])('rejects unsafe route params %j', (params, message) => {
    expect(normalizeResearchRouteParams(params)).toEqual({ ok: false, error: expect.stringMatching(message) });
  });

  it('previews agent_ready while selecting only the six bounded mobile tools', () => {
    expect(deriveResearchCapability(catalog)).toEqual({
      ready: true,
      agentReady: true,
      model: 'claude-sonnet',
      missingTools: [],
      message: 'Research access is ready.',
    });
    expect(deriveResearchCapability({ ...catalog, agentReady: false }).ready).toBe(false);
    expect(deriveResearchCapability({ ...catalog, tools: catalog.tools.slice(1) })).toMatchObject({
      ready: false,
      missingTools: ['analyze_ticker'],
    });
  });

  it('serializes ticker and period only inside bounded context with one fixed generic message', () => {
    const request = buildResearchRequest({
      symbol: ' aapl ',
      period: '1Y',
    });
    expect(buildResearchContext('AAPL', '1y')).toBe('Ticker: AAPL\nPeriod: 1y');
    expect(request).toEqual({
      messages: [{ role: 'user', content: RESEARCH_MESSAGE }],
      context: 'Ticker: AAPL\nPeriod: 1y',
    });
    expect(request).not.toHaveProperty('ticker');
    expect(request).not.toHaveProperty('period');
  });

  it('rejects a fallback trace that claims a tool outside the echoed boundary', () => {
    expect(() => completionFromAgentResult({
      symbol: 'AAPL',
      period: '1y',
      generatedAt: 100,
      result: {
        transport: 'fallback',
        state: { status: 'streaming', text: '', model: null, tools: [], events: [], error: null },
        fallback: {
          ok: true,
          model: 'claude-sonnet',
          tools: [...MOBILE_AGENT_TOOLS],
          text: 'Unsafe trace',
          stopReason: 'end_turn',
          toolCalls: [{ name: 'compose_article', ok: true, durationMs: 1, error: null }],
          toolTrace: [],
          articles: [],
          artifacts: [],
        },
      },
    })).toThrow(/outside/i);
  });
});
