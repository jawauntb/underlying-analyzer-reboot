import type {
  AgentChatResponse,
  AlertRow,
  AuctionResponse,
  ChartDataset,
  HealthResponse,
  MarketSnapshotResponse,
  OptionsChainResponse,
  OptionChainRow,
  ProviderStatusResponse,
  MoneylineResponse,
  ResolveWatchlistResponse,
  SecurityAssetType,
  SecuritySearchResponse,
  ToolCatalogResponse,
  TorqueResponse,
  Watchlist,
  WatchlistAlertsResponse,
} from './contracts';
import { exactMobileToolEcho } from './agentTools';
import { normalizeSymbol, normalizeSymbols } from './endpoints';

export class ContractError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'ContractError';
  }
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function record(value: unknown, label: string): Record<string, unknown> {
  if (!isRecord(value)) throw new ContractError(`${label} must be an object.`);
  return value;
}

function array(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function string(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}

function optionalNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function optionalText(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const normalized = value.trim();
  return normalized && normalized.toUpperCase() !== 'N/A' ? normalized : null;
}

function stringArray(value: unknown): string[] {
  return array(value).flatMap((item) => (typeof item === 'string' ? [item] : []));
}

function countMap(value: unknown): Record<string, number> {
  if (!isRecord(value)) return {};
  return Object.fromEntries(
    Object.entries(value).flatMap(([key, count]) => {
      const normalized = optionalNumber(count);
      return normalized === null ? [] : [[key, normalized]];
    }),
  );
}

function safeSymbol(value: unknown): string | null {
  try {
    return normalizeSymbol(string(value));
  } catch {
    return null;
  }
}

function safeSearchSymbol(value: unknown): string | null {
  return safeSymbol(value);
}

const SECURITY_ASSET_TYPES = new Set<SecurityAssetType>([
  'equity',
  'etf',
  'mutual_fund',
  'index',
  'crypto',
]);

export function isSecurityAssetType(value: unknown): value is SecurityAssetType {
  return typeof value === 'string' && SECURITY_ASSET_TYPES.has(value as SecurityAssetType);
}

function normalizeWatchlist(value: unknown): Watchlist | null {
  if (!isRecord(value)) return null;
  const sourceUrl = string(value.source_url ?? value.url);
  const name = string(value.name, 'Watchlist');
  const tickers = array(value.tickers).flatMap((ticker) => {
    const normalized = safeSymbol(ticker);
    return normalized ? [normalized] : [];
  });
  return {
    id: optionalNumber(value.id),
    name,
    sourceUrl,
    tickers,
  };
}

export function normalizeHealth(value: unknown): HealthResponse {
  const payload = record(value, 'Health response');
  if (payload.ok !== true || !string(payload.service)) {
    throw new ContractError('Health response is missing ok/service.');
  }
  return { ok: true, service: string(payload.service) };
}

export function normalizeMarketSnapshot(value: unknown): MarketSnapshotResponse {
  const payload = record(value, 'Market snapshot response');
  const ticker = safeSymbol(payload.ticker);
  const provider = string(payload.provider).trim();
  if (!ticker || !provider || !isRecord(payload.data)) {
    throw new ContractError('Market snapshot response is missing ticker/provider/data.');
  }
  return {
    ticker,
    provider,
    providerNote: string(payload.provider_note).trim() || null,
    data: payload.data,
  };
}

export function normalizeProviderStatus(value: unknown): ProviderStatusResponse {
  const payload = record(value, 'Provider status');
  const freshness = record(payload.freshness, 'Provider freshness');
  const streaming = record(payload.streaming, 'Provider streaming');
  const primary = string(payload.primary).trim();
  if (!primary) throw new ContractError('Provider status is missing primary provider.');
  return {
    primary,
    fallback: string(payload.fallback).trim() || null,
    massiveConfigured: payload.massive_configured === true,
    fallbackEnabled: payload.fallback_enabled === true,
    freshness: {
      stocks: string(freshness.stocks, 'unknown'),
      options: string(freshness.options, 'unknown'),
    },
    streaming: {
      enabled: streaming.enabled === true,
      configured: streaming.configured === true,
      transport: string(streaming.transport, 'unavailable'),
      freshness: string(streaming.freshness, 'unknown'),
      endpoint: string(streaming.endpoint),
    },
    notes: stringArray(payload.notes),
  };
}

function optionRow(value: unknown): OptionChainRow | null {
  if (!isRecord(value)) return null;
  const strike = optionalNumber(value.strike);
  if (strike === null) return null;
  return {
    strike,
    callOpenInterest: optionalNumber(value.call_open_interest) ?? 0,
    putOpenInterest: optionalNumber(value.put_open_interest) ?? 0,
    callLast: optionalNumber(value.call_last) ?? 0,
    putLast: optionalNumber(value.put_last) ?? 0,
    callImpliedVolatility: optionalNumber(value.call_implied_volatility),
    putImpliedVolatility: optionalNumber(value.put_implied_volatility),
    callDelta: optionalNumber(value.call_delta),
    putDelta: optionalNumber(value.put_delta),
    callBid: optionalNumber(value.call_bid),
    callAsk: optionalNumber(value.call_ask),
    putBid: optionalNumber(value.put_bid),
    putAsk: optionalNumber(value.put_ask),
    callVolume: optionalNumber(value.call_volume),
    putVolume: optionalNumber(value.put_volume),
  };
}

export function normalizeOptionsChain(value: unknown): OptionsChainResponse {
  const payload = record(value, 'Options chain response');
  const ticker = safeSymbol(payload.ticker);
  const expiry = string(payload.expiry).trim();
  const provider = string(payload.provider).trim();
  if (!ticker || !expiry || !provider) {
    throw new ContractError('Options chain response is missing ticker/expiry/provider.');
  }
  const expirations = stringArray(payload.expirations);
  const rows = array(payload.rows).flatMap((item) => {
    const row = optionRow(item);
    return row ? [row] : [];
  });
  return {
    ticker,
    expiry,
    currentPrice: optionalNumber(payload.current_price),
    expirations,
    rows,
    provider,
    providerNote: string(payload.provider_note).trim() || null,
  };
}

export function normalizeToolCatalog(value: unknown): ToolCatalogResponse {
  const payload = record(value, 'Tool catalog');
  const tools = array(payload.tools).flatMap((item) => {
    if (!isRecord(item) || !string(item.name)) return [];
    return [
      {
        name: string(item.name),
        title: string(item.title, string(item.name)),
        group: string(item.group, 'unknown'),
        summary: string(item.summary ?? item.description),
        whenToUse: string(item.when_to_use),
        returns: string(item.returns),
        cost: string(item.cost, 'unknown'),
        producesImages: item.produces_images === true,
        agent: item.agent === true,
        mcp: item.mcp === true,
        http: {
          method: isRecord(item.http) ? string(item.http.method, 'GET') : 'GET',
          path: isRecord(item.http) ? string(item.http.path) : '',
        },
        arguments: stringArray(item.arguments),
        required: stringArray(item.required),
      },
    ];
  });
  return {
    agentReady: payload.agent_ready === true,
    model: string(payload.model) || null,
    toolCount: optionalNumber(payload.tool_count) ?? tools.length,
    tools,
  };
}

export function normalizeResolvedWatchlist(value: unknown): ResolveWatchlistResponse {
  const payload = record(value, 'Watchlist response');
  const watchlist = normalizeWatchlist(payload.watchlist);
  if (!watchlist) throw new ContractError('Watchlist response is missing watchlist data.');
  const tickers = normalizeSymbols(
    array(payload.tickers).flatMap((value) => {
      const ticker = safeSymbol(value);
      return ticker ? [ticker] : [];
    }),
  );
  if (!tickers.length) throw new ContractError('Watchlist response has no valid tickers.');
  return {
    watchlist,
    tickers,
    maxResults: optionalNumber(payload.max_results) ?? 10,
  };
}

export function normalizeSecuritySearch(value: unknown): SecuritySearchResponse {
  const payload = record(value, 'Security search response');
  const query = string(payload.query).trim();
  const provider = string(payload.provider).trim();
  if (!query || !provider) {
    throw new ContractError('Security search response is missing query/provider.');
  }
  const results = array(payload.results).flatMap((item) => {
    if (!isRecord(item)) return [];
    const symbol = safeSearchSymbol(item.symbol);
    const assetType = string(item.asset_type) as SecurityAssetType;
    if (
      !symbol
      || typeof item.name !== 'string'
      || typeof item.exchange !== 'string'
      || !isSecurityAssetType(assetType)
    ) return [];
    return [{
      symbol,
      name: item.name.trim(),
      exchange: item.exchange.trim(),
      assetType,
    }];
  });
  return { query, results, provider };
}

function normalizeErrors(meta: Record<string, unknown>): { ticker: string; error: string }[] {
  return array(meta.errors).flatMap((item) => {
    if (!isRecord(item)) return [];
    const ticker = safeSymbol(item.ticker);
    const error = string(item.error);
    return ticker && error ? [{ ticker, error }] : [];
  });
}

export function normalizeWatchlistAlerts(value: unknown): WatchlistAlertsResponse {
  const payload = record(value, 'Alerts response');
  const meta = isRecord(payload.meta) ? payload.meta : {};
  const errors = normalizeErrors(meta);
  const rows: AlertRow[] = array(payload.rows).flatMap((item) => {
    if (!isRecord(item)) return [];
    const ticker = safeSymbol(item.ticker);
    if (!ticker) return [];
    return [
      {
        ticker,
        rank: optionalNumber(item.rank) ?? undefined,
        lane: string(item.lane) || undefined,
        name: string(item.name) || null,
        sector: string(item.sector) || null,
        industry: string(item.industry) || null,
        price: optionalNumber(item.price),
        changePercent: optionalNumber(item.change_percent),
        annualVolatility: optionalNumber(item.annual_volatility),
        trend50d: optionalNumber(item.trend_50d),
        distanceFrom52WeekHigh: optionalNumber(item.distance_from_52w_high),
        distanceFrom52WeekLow: optionalNumber(item.distance_from_52w_low),
        scannerScore: optionalNumber(item.scanner_score),
        score: optionalNumber(item.score),
        setup: string(item.setup) || null,
        provider: string(item.provider) || null,
        providerNote: string(item.provider_note) || null,
        fundamentals: (() => {
          const summary = isRecord(item.summary) ? item.summary : {};
          return {
            businessSummary: optionalText(summary.business_summary),
            country: optionalText(summary.country),
            website: optionalText(summary.website),
            employees: optionalNumber(summary.employees),
            marketCap: optionalText(summary.market_cap),
            trailingPe: optionalNumber(summary.trailing_pe),
            forwardPe: optionalNumber(summary.forward_pe),
            priceToSales: optionalNumber(summary.price_to_sales),
            priceToBook: optionalNumber(summary.price_to_book),
            revenueGrowth: optionalNumber(summary.revenue_growth),
            profitMargins: optionalNumber(summary.profit_margins),
            returnOnEquity: optionalNumber(summary.return_on_equity),
            debtToEquity: optionalNumber(summary.debt_to_equity),
            recommendation: optionalText(summary.recommendation),
            targetMeanPrice: optionalNumber(summary.target_mean_price),
            analystCount: optionalNumber(summary.analyst_count),
            beta: optionalNumber(summary.beta),
            fiftyTwoWeekHigh: optionalNumber(summary.fifty_two_week_high),
            fiftyTwoWeekLow: optionalNumber(summary.fifty_two_week_low),
          };
        })(),
        ridge: isRecord(item.ridge) ? item.ridge : {},
        flow: isRecord(item.flow) ? item.flow : {},
        auction: isRecord(item.auction) ? item.auction : {},
        raw: item,
      },
    ];
  });
  const alerts = array(payload.alerts).flatMap((item) => {
    if (!isRecord(item)) return [];
    const ticker = safeSymbol(item.ticker);
    const id = string(item.id);
    if (!ticker || !id) return [];
    return [{
      id,
      ticker,
      rank: optionalNumber(item.rank),
      lane: string(item.lane, 'Review'),
      score: optionalNumber(item.score),
      severity: string(item.severity, 'Info'),
      category: string(item.category),
      title: string(item.title),
      message: string(item.message),
      action: string(item.action),
    }];
  });
  const digest = isRecord(payload.digest) ? payload.digest : {};
  return {
    status: errors.length ? 'partial' : 'fresh',
    rows,
    alerts,
    digest: {
      generatedAt: string(digest.generated_at) || null,
      headline: string(digest.headline),
      summary: string(digest.summary),
      severityCounts: countMap(digest.severity_counts),
      categoryCounts: countMap(digest.category_counts),
      laneCounts: countMap(digest.lane_counts),
      priorityTickers: stringArray(digest.priority_tickers).flatMap((ticker) => {
        const normalized = safeSymbol(ticker);
        return normalized ? [normalized] : [];
      }),
      riskTickers: stringArray(digest.risk_tickers).flatMap((ticker) => {
        const normalized = safeSymbol(ticker);
        return normalized ? [normalized] : [];
      }),
      flowShiftTickers: stringArray(digest.flow_shift_tickers).flatMap((ticker) => {
        const normalized = safeSymbol(ticker);
        return normalized ? [normalized] : [];
      }),
      nextSteps: stringArray(digest.next_steps),
    },
    provider: string(payload.provider) || null,
    providerNote: string(payload.provider_note) || null,
    errors,
    meta,
    watchlist: normalizeWatchlist(payload.watchlist),
    tickers: array(payload.tickers).flatMap((ticker) => {
      const normalized = safeSymbol(ticker);
      return normalized ? [normalized] : [];
    }),
  };
}

function normalizeDataset(value: unknown): ChartDataset {
  const payload = record(value, 'Chart dataset');
  const ticker = safeSymbol(payload.ticker);
  const tickers = array(payload.tickers).flatMap((item) => {
    const normalized = safeSymbol(item);
    return normalized ? [normalized] : [];
  });
  const chartType = string(payload.chart_type);
  if (!chartType) throw new ContractError('Chart dataset is missing chart_type.');
  return {
    chartType,
    ticker: ticker ?? undefined,
    tickers: tickers.length ? tickers : undefined,
    period: string(payload.period) || undefined,
    meta: isRecord(payload.meta) ? payload.meta : {},
    levels: isRecord(payload.levels) ? payload.levels : {},
    series: isRecord(payload.series) ? payload.series : {},
    rows: array(payload.rows).filter(isRecord),
    raw: payload,
  };
}

export function normalizeAuction(value: unknown): AuctionResponse {
  const payload = record(value, 'Auction response');
  const meta = isRecord(payload.meta) ? payload.meta : {};
  const errors = normalizeErrors(meta);
  return {
    datasets: array(payload.datasets).map(normalizeDataset),
    provider: string(payload.provider) || null,
    providerNote: string(payload.provider_note) || null,
    errors,
    meta,
    status: errors.length ? 'partial' : 'fresh',
  };
}

export function normalizeTorque(value: unknown): TorqueResponse {
  const dataset = normalizeDataset(value);
  if (dataset.chartType !== 'torque' || !dataset.ticker) {
    throw new ContractError('Torque response is missing a valid ticker or chart type.');
  }
  const payload = value as Record<string, unknown>;
  return {
    ...dataset,
    chartType: 'torque',
    ticker: dataset.ticker,
    torque: isRecord(payload.torque) ? payload.torque : {},
  };
}

export function normalizeMoneyline(value: unknown): MoneylineResponse {
  const payload = record(value, 'Moneyline response');
  const ticker = safeSymbol(payload.ticker ?? (isRecord(payload.meta) ? payload.meta.ticker : null));
  if (!ticker || string(payload.chart_type) !== 'moneyline') {
    throw new ContractError('Moneyline response is missing a valid ticker or chart type.');
  }
  const seriesRows = isRecord(payload.series) ? payload.series.strikes : undefined;
  const metaRows = isRecord(payload.meta) ? payload.meta.rows : undefined;
  const sourceRows = array(payload.rows).length ? payload.rows : array(seriesRows).length ? seriesRows : metaRows;
  const rows = array(sourceRows).flatMap((item) => {
    if (!isRecord(item)) return [];
    const strike = optionalNumber(item.strike);
    if (strike === null) return [];
    return [
      {
        strike,
        callOpenInterest: optionalNumber(item.call_open_interest),
        putOpenInterest: optionalNumber(item.put_open_interest),
        callLast: optionalNumber(item.call_last),
        putLast: optionalNumber(item.put_last),
        netOpenInterest: optionalNumber(item.net_open_interest),
        putCallRatio: optionalNumber(item.put_call_ratio),
        raw: item,
      },
    ];
  });
  return {
    chartType: 'moneyline',
    ticker,
    meta: isRecord(payload.meta) ? payload.meta : {},
    rows,
  };
}

function stripBinary(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stripBinary);
  if (!isRecord(value)) return value;
  return Object.fromEntries(
    Object.entries(value)
      .filter(([key]) => !['data', 'base64', 'bytes'].includes(key.toLowerCase()))
      .map(([key, child]) => [key, stripBinary(child)]),
  );
}

export function normalizeAgentChat(value: unknown): AgentChatResponse {
  const payload = record(value, 'Agent response');
  if (typeof payload.error === 'string' && payload.error) {
    throw new ContractError(payload.error);
  }
  if (payload.ok !== true) throw new ContractError('Agent response is missing ok.');
  const model = string(payload.model);
  if (!model) throw new ContractError('Agent response is missing model.');
  const tools = exactMobileToolEcho(payload.tools);
  if (!tools) throw new ContractError('Agent tool allowlist mismatch.');
  return {
    ok: true,
    model,
    tools,
    text: string(payload.text),
    stopReason: string(payload.stop_reason, 'end_turn'),
    toolCalls: array(payload.tool_calls).flatMap((item) => {
      if (!isRecord(item) || !string(item.name)) return [];
      return [
        {
          name: string(item.name),
          ok: typeof item.ok === 'boolean' ? item.ok : null,
          durationMs: optionalNumber(item.duration_ms),
          error: string(item.error) || null,
        },
      ];
    }),
    toolTrace: array(payload.tool_trace).map(String),
    articles: array(payload.articles).filter(isRecord),
    artifacts: array(payload.artifacts).filter(isRecord).map((artifact) => stripBinary(artifact) as Record<string, unknown>),
  };
}
