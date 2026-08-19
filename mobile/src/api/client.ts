import type {
  AgentChatRequest,
  AgentChatResponse,
  AuctionRequest,
  AuctionResponse,
  HealthResponse,
  MoneylineRequest,
  MoneylineResponse,
  ResolveWatchlistRequest,
  ResolveWatchlistResponse,
  SecuritySearchRequest,
  SecuritySearchResponse,
  ToolCatalogResponse,
  TorqueRequest,
  TorqueResponse,
  WatchlistAlertsRequest,
  WatchlistAlertsResponse,
} from './contracts';
import { MOBILE_AGENT_TOOLS } from './agentTools';
import { API_ENDPOINTS, buildApiConfig, endpointUrl, normalizeSymbol, normalizeSymbols } from './endpoints';
import {
  ContractError,
  isRecord,
  normalizeAgentChat,
  normalizeAuction,
  normalizeHealth,
  normalizeMoneyline,
  normalizeResolvedWatchlist,
  normalizeSecuritySearch,
  normalizeToolCatalog,
  normalizeTorque,
  normalizeWatchlistAlerts,
} from './guards';
import { AgentStreamState, NdjsonParser, NdjsonProtocolError } from './ndjson';
import { runtimeFetch, type RuntimeFetch } from './runtimeFetch';

export const TIMEOUT_MS = {
  normal: 30_000,
  capability: 10_000,
  researchIdle: 45_000,
} as const;

export type ApiErrorKind =
  | 'config'
  | 'validation'
  | 'network'
  | 'timeout'
  | 'cancelled'
  | 'http'
  | 'api'
  | 'protocol';

export class ApiError extends Error {
  constructor(
    readonly kind: ApiErrorKind,
    message: string,
    readonly status?: number,
    options?: ErrorOptions,
  ) {
    super(message, options);
    this.name = 'ApiError';
  }
}

type ClientOptions = {
  baseUrl?: string;
  fetchImpl?: RuntimeFetch;
};

type RequestOptions = {
  method?: 'GET' | 'POST';
  body?: unknown;
  query?: Record<string, string>;
  timeoutMs?: number;
  signal?: AbortSignal;
  controller?: AbortController;
};

export type AgentStreamResult = {
  transport: 'stream' | 'fallback';
  state: ReturnType<AgentStreamState['snapshot']>;
  fallback?: AgentChatResponse;
};

export type AgentStreamSession = {
  controller: AbortController;
  result: Promise<AgentStreamResult>;
  cancel: () => void;
};

function createLinkedController(signal?: AbortSignal): { controller: AbortController; dispose: () => void } {
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (signal?.aborted) controller.abort();
  else signal?.addEventListener('abort', abort, { once: true });
  return {
    controller,
    dispose: () => signal?.removeEventListener('abort', abort),
  };
}

function errorMessageFrom(value: unknown): string | null {
  return isRecord(value) && typeof value.error === 'string' && value.error ? value.error : null;
}

async function readFailure(response: Response): Promise<ApiError> {
  const contentType = response.headers.get('content-type')?.toLowerCase() ?? '';
  if (contentType.includes('application/json')) {
    try {
      const payload: unknown = await response.json();
      return new ApiError('http', errorMessageFrom(payload) ?? `Request failed (HTTP ${response.status}).`, response.status);
    } catch {
      return new ApiError('http', `The service returned invalid JSON (HTTP ${response.status}).`, response.status);
    }
  }
  await response.text().catch(() => '');
  return new ApiError('http', `The service returned a non-JSON error (HTTP ${response.status}).`, response.status);
}

function serializeAlertsRequest(request: WatchlistAlertsRequest): Record<string, unknown> {
  const body: Record<string, unknown> = {};
  if (request.ticker) body.ticker = normalizeSymbol(request.ticker);
  if (request.tickers) body.tickers = normalizeSymbols(request.tickers);
  if (request.watchlistUrl) body.watchlist_url = request.watchlistUrl.trim();
  if (request.maxResults !== undefined) body.max_results = request.maxResults;
  if (request.maxAlerts !== undefined) body.max_alerts = request.maxAlerts;
  if (request.volatilityThreshold !== undefined) body.volatility_threshold = request.volatilityThreshold;
  if (request.period) body.period = request.period;
  return body;
}

function normalizeSearchRequest(request: SecuritySearchRequest): { query: string; limit?: number } {
  const query = typeof request.query === 'string' ? request.query.trim() : '';
  if (!query) throw new ApiError('validation', 'Search query is required.');
  if (query.length > 100) throw new ApiError('validation', 'Search query must be at most 100 characters.');
  if (request.limit !== undefined && (!Number.isInteger(request.limit) || request.limit < 1 || request.limit > 10)) {
    throw new ApiError('validation', 'Search limit must be an integer from 1 to 10.');
  }
  return { query, limit: request.limit };
}

function boundedAgentRequest(request: AgentChatRequest): Record<string, unknown> {
  const messages = request.messages.slice(-40).map((message) => ({
    role: message.role,
    content: message.content.trim().slice(0, 12_000),
  }));
  if (!messages.length) throw new ApiError('validation', 'At least one agent message is required.');
  return {
    messages,
    tools: [...MOBILE_AGENT_TOOLS],
    tool_policy: 'exact',
    ...(request.context ? { context: request.context.trim().slice(0, 2_000) } : {}),
  };
}

export class ApiClient {
  readonly baseUrl: string;
  private readonly fetchImpl: RuntimeFetch;

  constructor(options: ClientOptions = {}) {
    const config = buildApiConfig(options.baseUrl);
    if (config.status !== 'configured') throw new ApiError('config', config.message);
    this.baseUrl = config.baseUrl;
    this.fetchImpl = options.fetchImpl ?? runtimeFetch;
  }

  health(options: { signal?: AbortSignal } = {}): Promise<HealthResponse> {
    return this.getJson(API_ENDPOINTS.health, normalizeHealth, {
      timeoutMs: TIMEOUT_MS.capability,
      signal: options.signal,
    });
  }

  tools(options: { signal?: AbortSignal } = {}): Promise<ToolCatalogResponse> {
    return this.getJson(API_ENDPOINTS.tools, normalizeToolCatalog, {
      timeoutMs: TIMEOUT_MS.capability,
      signal: options.signal,
    });
  }

  async searchSecurities(
    request: SecuritySearchRequest,
    options: { signal?: AbortSignal } = {},
  ): Promise<SecuritySearchResponse> {
    const normalized = normalizeSearchRequest(request);
    return this.getJson(API_ENDPOINTS.search, normalizeSecuritySearch, {
      query: {
        q: normalized.query,
        ...(normalized.limit === undefined ? {} : { limit: String(normalized.limit) }),
      },
      timeoutMs: TIMEOUT_MS.capability,
      signal: options.signal,
    });
  }

  resolveWatchlist(request: ResolveWatchlistRequest, options: { signal?: AbortSignal } = {}): Promise<ResolveWatchlistResponse> {
    return this.getJson(API_ENDPOINTS.resolveWatchlist, normalizeResolvedWatchlist, {
      method: 'POST',
      body: {
        watchlist_url: request.watchlistUrl.trim(),
        ...(request.maxResults === undefined ? {} : { max_results: request.maxResults }),
      },
      signal: options.signal,
    });
  }

  watchlistAlerts(request: WatchlistAlertsRequest, options: { signal?: AbortSignal } = {}): Promise<WatchlistAlertsResponse> {
    return this.getJson(API_ENDPOINTS.alerts, normalizeWatchlistAlerts, {
      method: 'POST',
      body: serializeAlertsRequest(request),
      signal: options.signal,
    });
  }

  auction(request: AuctionRequest, options: { signal?: AbortSignal } = {}): Promise<AuctionResponse> {
    return this.getJson(API_ENDPOINTS.auction, normalizeAuction, {
      method: 'POST',
      body: serializeAlertsRequest(request),
      signal: options.signal,
    });
  }

  torque(request: TorqueRequest, options: { signal?: AbortSignal } = {}): Promise<TorqueResponse> {
    return this.getJson(API_ENDPOINTS.torque, normalizeTorque, {
      method: 'POST',
      body: { ticker: normalizeSymbol(request.ticker) },
      signal: options.signal,
    });
  }

  moneyline(request: MoneylineRequest, options: { signal?: AbortSignal } = {}): Promise<MoneylineResponse> {
    return this.getJson(API_ENDPOINTS.moneyline, normalizeMoneyline, {
      method: 'POST',
      body: {
        ticker: normalizeSymbol(request.ticker),
        ...(request.expiry ? { expiry: request.expiry } : {}),
      },
      signal: options.signal,
    });
  }

  agentChat(request: AgentChatRequest, options: { signal?: AbortSignal } = {}): Promise<AgentChatResponse> {
    return this.getJson(API_ENDPOINTS.agentChat, normalizeAgentChat, {
      method: 'POST',
      body: boundedAgentRequest(request),
      timeoutMs: TIMEOUT_MS.researchIdle,
      signal: options.signal,
    });
  }

  agentStream(
    request: AgentChatRequest,
    options: { signal?: AbortSignal; onEvent?: (event: unknown) => void } = {},
  ): AgentStreamSession {
    const linked = createLinkedController(options.signal);
    const state = new AgentStreamState();
    const result = this.runAgentStream(request, linked.controller, state, options.onEvent).finally(linked.dispose);
    return {
      controller: linked.controller,
      result,
      cancel: () => {
        state.cancel();
        linked.controller.abort();
      },
    };
  }

  private async runAgentStream(
    request: AgentChatRequest,
    controller: AbortController,
    state: AgentStreamState,
    onEvent?: (event: unknown) => void,
  ): Promise<AgentStreamResult> {
    let idleTimer: ReturnType<typeof setTimeout> | undefined;
    const clearIdle = () => {
      if (idleTimer === undefined) return;
      clearTimeout(idleTimer);
      idleTimer = undefined;
    };
    const resetIdle = () => {
      clearIdle();
      idleTimer = setTimeout(() => controller.abort('research-idle-timeout'), TIMEOUT_MS.researchIdle);
    };
    try {
      resetIdle();
      const response = await this.fetchImpl(endpointUrl(this.baseUrl, API_ENDPOINTS.agentStream), {
        method: 'POST',
        headers: { Accept: 'application/x-ndjson', 'Content-Type': 'application/json' },
        body: JSON.stringify(boundedAgentRequest(request)),
        signal: controller.signal,
      });
      if (!response.ok) {
        if ([404, 405, 501].includes(response.status)) {
          clearIdle();
          const fallback = await this.getJson(API_ENDPOINTS.agentChat, normalizeAgentChat, {
            method: 'POST',
            body: boundedAgentRequest(request),
            timeoutMs: TIMEOUT_MS.researchIdle,
            controller,
          });
          return { transport: 'fallback', state: state.snapshot(), fallback };
        }
        throw await readFailure(response);
      }
      const contentType = response.headers.get('content-type')?.toLowerCase() ?? '';
      if (!contentType.includes('application/x-ndjson')) {
        throw new ApiError('protocol', 'Agent stream did not return application/x-ndjson.', response.status);
      }
      if (!response.body) throw new ApiError('protocol', 'Agent stream response has no readable body.');

      const parser = new NdjsonParser((value) => {
        const event = state.accept(value);
        if (event) onEvent?.(event);
      });
      const reader = response.body.getReader();
      try {
        while (true) {
          const chunk = await reader.read();
          if (chunk.done) break;
          resetIdle();
          parser.push(chunk.value);
        }
        parser.finish();
        state.finish();
      } catch (error) {
        if (controller.signal.aborted) {
          parser.abort();
          if (controller.signal.reason === 'research-idle-timeout') {
            state.fail('Agent stream timed out while waiting for data.');
            throw new ApiError('timeout', 'The agent stream timed out while waiting for data.', undefined, {
              cause: error,
            });
          }
          state.cancel();
          throw new ApiError('cancelled', 'Agent stream was cancelled.', undefined, { cause: error });
        }
        parser.abort();
        await reader.cancel(error).catch(() => undefined);
        throw error;
      } finally {
        reader.releaseLock();
      }
      return { transport: 'stream', state: state.snapshot() };
    } catch (error) {
      if (error instanceof ApiError) throw error;
      if (error instanceof NdjsonProtocolError || error instanceof ContractError) {
        state.fail(error.message);
        throw new ApiError('protocol', error.message, undefined, { cause: error });
      }
      throw this.toApiError(error, controller.signal, controller.signal.reason === 'research-idle-timeout');
    } finally {
      clearIdle();
    }
  }

  private async getJson<T>(
    endpoint: string,
    normalize: (value: unknown) => T,
    options: RequestOptions = {},
  ): Promise<T> {
    const linked = options.controller
      ? { controller: options.controller, dispose: () => undefined }
      : createLinkedController(options.signal);
    const timeoutMs = options.timeoutMs ?? TIMEOUT_MS.normal;
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      linked.controller.abort('timeout');
    }, timeoutMs);
    try {
      const response = await this.fetchImpl(endpointUrl(this.baseUrl, endpoint, options.query), {
        method: options.method ?? 'GET',
        headers: { Accept: 'application/json', ...(options.body === undefined ? {} : { 'Content-Type': 'application/json' }) },
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
        signal: linked.controller.signal,
      });
      if (!response.ok) throw await readFailure(response);
      const contentType = response.headers.get('content-type')?.toLowerCase() ?? '';
      if (!contentType.includes('application/json')) {
        throw new ApiError('protocol', 'The service returned a non-JSON success response.', response.status);
      }
      let payload: unknown;
      try {
        payload = await response.json();
      } catch (error) {
        throw new ApiError('protocol', 'The service returned invalid JSON.', response.status, { cause: error });
      }
      const apiMessage = errorMessageFrom(payload);
      if (apiMessage) throw new ApiError('api', apiMessage, response.status);
      try {
        return normalize(payload);
      } catch (error) {
        if (error instanceof ContractError) throw new ApiError('protocol', error.message, response.status, { cause: error });
        throw error;
      }
    } catch (error) {
      if (error instanceof ApiError) throw error;
      throw this.toApiError(error, linked.controller.signal, timedOut);
    } finally {
      clearTimeout(timer);
      linked.dispose();
    }
  }

  private toApiError(error: unknown, signal: AbortSignal, timedOut: boolean): ApiError {
    if (timedOut) return new ApiError('timeout', 'The request timed out.', undefined, { cause: error });
    if (signal.aborted || (error instanceof DOMException && error.name === 'AbortError')) {
      return new ApiError('cancelled', 'The request was cancelled.', undefined, { cause: error });
    }
    if (error instanceof Error && /symbol/i.test(error.message)) {
      return new ApiError('validation', error.message, undefined, { cause: error });
    }
    return new ApiError('network', 'The service could not be reached.', undefined, { cause: error });
  }
}

export type CoordinatedResult<T> = { accepted: boolean; value: T };

export class RequestCoordinator<T> {
  private generation = 0;
  private controller: AbortController | null = null;

  async run(task: (signal: AbortSignal) => Promise<T>): Promise<CoordinatedResult<T>> {
    this.controller?.abort();
    const controller = new AbortController();
    this.controller = controller;
    const generation = ++this.generation;
    const value = await task(controller.signal);
    return { accepted: generation === this.generation && !controller.signal.aborted, value };
  }

  cancel(): void {
    this.generation += 1;
    this.controller?.abort();
    this.controller = null;
  }
}
