import { exactMobileToolEcho, MOBILE_AGENT_TOOLS } from '@/src/api/agentTools';
import type { AgentStreamResult } from '@/src/api/client';
import type { AgentChatRequest, AgentStreamEvent, ToolCatalogResponse } from '@/src/api/contracts';
import { normalizeSymbol } from '@/src/api/endpoints';
import type { ResearchCompletion, ResearchTraceEntry } from '@/src/features/library/library-store';

export const RESEARCH_PERIODS = ['5d', '1mo', '3mo', '6mo', '1y', '2y', '5y', '10y'] as const;
export type ResearchPeriod = (typeof RESEARCH_PERIODS)[number];

export const RESEARCH_MESSAGE =
  'Run the bounded mobile research workflow. Summarize the evidence, uncertainty, and provider provenance.';

type RouteValue = string | string[] | undefined;

export type ResearchRouteParams = {
  symbol?: RouteValue;
  period?: RouteValue;
  recordId?: RouteValue;
};

export type NormalizedResearchRoute =
  | { ok: true; symbol: string; period: ResearchPeriod; recordId: string | null }
  | { ok: false; error: string };

export type ResearchCapability = {
  ready: boolean;
  agentReady: boolean;
  model: string | null;
  missingTools: string[];
  message: string;
};

function singleRouteValue(value: RouteValue): string | null {
  return typeof value === 'string' ? value : null;
}

export function normalizeResearchPeriod(value: string): ResearchPeriod {
  const period = value.trim().toLowerCase();
  if (!(RESEARCH_PERIODS as readonly string[]).includes(period)) {
    throw new Error(`Period must be one of ${RESEARCH_PERIODS.join(', ')}.`);
  }
  return period as ResearchPeriod;
}

export function normalizeResearchRouteParams(params: ResearchRouteParams): NormalizedResearchRoute {
  const rawSymbol = singleRouteValue(params.symbol);
  if (!rawSymbol) return { ok: false, error: 'Choose one valid ticker before opening Research Run.' };

  let symbol: string;
  try {
    symbol = normalizeSymbol(rawSymbol);
  } catch {
    return { ok: false, error: 'The Research Run ticker is invalid.' };
  }

  let period: ResearchPeriod;
  try {
    const rawPeriod = params.period === undefined ? '1y' : singleRouteValue(params.period);
    if (!rawPeriod) throw new Error('invalid period');
    period = normalizeResearchPeriod(rawPeriod);
  } catch {
    return { ok: false, error: `The Research Run period must be one of ${RESEARCH_PERIODS.join(', ')}.` };
  }

  const rawRecordId = params.recordId === undefined ? null : singleRouteValue(params.recordId);
  if (params.recordId !== undefined && (!rawRecordId || !/^[A-Za-z0-9._-]{1,128}$/.test(rawRecordId))) {
    return { ok: false, error: 'The saved research identifier is invalid.' };
  }

  return { ok: true, symbol, period, recordId: rawRecordId };
}

export function deriveResearchCapability(catalog: ToolCatalogResponse): ResearchCapability {
  const agentNames = new Set(catalog.tools.filter((tool) => tool.agent).map((tool) => tool.name));
  const missingTools = MOBILE_AGENT_TOOLS.filter((tool) => !agentNames.has(tool));
  const ready = catalog.agentReady && missingTools.length === 0;
  return {
    ready,
    agentReady: catalog.agentReady,
    model: catalog.model,
    missingTools,
    message: ready
      ? 'Research access is ready.'
      : !catalog.agentReady
        ? 'The research agent is not ready.'
        : `Required tools are unavailable: ${missingTools.join(', ')}.`,
  };
}

export function buildResearchContext(symbol: string, period: string): string {
  return `Ticker: ${normalizeSymbol(symbol)}\nPeriod: ${normalizeResearchPeriod(period)}`;
}

export function buildResearchRequest(input: { symbol: string; period: string }): AgentChatRequest {
  return {
    messages: [{ role: 'user', content: RESEARCH_MESSAGE }],
    context: buildResearchContext(input.symbol, input.period),
  };
}

function traceFromEvents(events: readonly AgentStreamEvent[]): ResearchTraceEntry[] {
  const entries: { id: string; trace: ResearchTraceEntry }[] = [];
  events.forEach((event) => {
    if (event.type === 'tool_call') {
      entries.push({
        id: event.id,
        trace: { name: event.name, status: 'started', durationMs: null, error: null },
      });
    } else if (event.type === 'tool_result') {
      const existing = entries.find((entry) => entry.id === event.id);
      const trace: ResearchTraceEntry = {
        name: event.name,
        status: event.ok ? 'completed' : 'failed',
        durationMs: event.durationMs ?? null,
        error: event.error ?? null,
      };
      if (existing) existing.trace = trace;
      else entries.push({ id: event.id, trace });
    }
  });
  return entries.map((entry) => entry.trace);
}

export function completionFromAgentResult(input: {
  result: AgentStreamResult;
  symbol: string;
  period: ResearchPeriod;
  generatedAt: number;
}): ResearchCompletion {
  const { result } = input;
  if (result.transport === 'fallback') {
    if (!result.fallback) throw new Error('The fallback response is missing.');
    const tools = exactMobileToolEcho(result.fallback.tools);
    if (!tools) throw new Error('The fallback tool allowlist does not match this run.');
    const expandedCall = result.fallback.toolCalls.find(
      (call) => !(MOBILE_AGENT_TOOLS as readonly string[]).includes(call.name),
    );
    if (expandedCall) throw new Error(`Fallback tool ${expandedCall.name} is outside the mobile allowlist.`);
    return {
      status: 'completed',
      symbol: normalizeSymbol(input.symbol),
      period: input.period,
      summary: result.fallback.text,
      model: result.fallback.model,
      tools,
      toolTrace: result.fallback.toolCalls.map((call) => ({
        name: call.name,
        status: call.ok === false ? 'failed' : call.ok === true ? 'completed' : 'started',
        durationMs: call.durationMs,
        error: call.error,
      })),
      artifacts: result.fallback.artifacts,
      generatedAt: input.generatedAt,
      transport: 'fallback',
    };
  }

  if (result.state.status !== 'completed' || !result.state.model) {
    throw new Error('The research stream did not reach a terminal completed state.');
  }
  const tools = exactMobileToolEcho(result.state.tools);
  if (!tools) throw new Error('The streamed tool allowlist does not match this run.');
  return {
    status: 'completed',
    symbol: normalizeSymbol(input.symbol),
    period: input.period,
    summary: result.state.text,
    model: result.state.model,
    tools,
    toolTrace: traceFromEvents(result.state.events),
    artifacts: result.state.events.flatMap((event) => event.type === 'tool_result' ? event.artifacts : []),
    generatedAt: input.generatedAt,
    transport: 'stream',
  };
}
