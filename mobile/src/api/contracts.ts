export type JsonPrimitive = string | number | boolean | null;
export type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };

export type HealthResponse = {
  ok: true;
  service: string;
};

export type MarketSnapshotResponse = {
  ticker: string;
  provider: string;
  providerNote: string | null;
  data: Record<string, unknown>;
};

export type ApiCatalogEndpoint = {
  method: string;
  path: string;
  group?: string;
  summary?: string;
};

export type ToolCatalogItem = {
  name: string;
  title: string;
  group: string;
  summary: string;
  whenToUse: string;
  returns: string;
  cost: string;
  producesImages: boolean;
  agent: boolean;
  mcp: boolean;
  http: { method: string; path: string };
  arguments: string[];
  required: string[];
};

export type ToolCatalogResponse = {
  agentReady: boolean;
  model: string | null;
  toolCount: number;
  tools: ToolCatalogItem[];
};

export type ResolveWatchlistRequest = {
  watchlistUrl: string;
  maxResults?: number;
};

export type Watchlist = {
  id?: number | null;
  name: string;
  sourceUrl: string;
  tickers: string[];
};

export type ResolveWatchlistResponse = {
  watchlist: Watchlist;
  tickers: string[];
  maxResults: number;
};

export type SecuritySearchRequest = {
  query: string;
  limit?: number;
};

export type SecurityAssetType = 'equity' | 'etf' | 'mutual_fund' | 'index' | 'crypto';

export type SecuritySearchResult = {
  symbol: string;
  name: string;
  exchange: string;
  assetType: SecurityAssetType;
};

export type SecuritySearchResponse = {
  query: string;
  results: SecuritySearchResult[];
  provider: string;
};

export type WatchlistAlertsRequest = {
  ticker?: string;
  tickers?: string[];
  watchlistUrl?: string;
  maxResults?: number;
  maxAlerts?: number;
  volatilityThreshold?: number;
  period?: string;
};

export type PerTickerError = {
  ticker: string;
  error: string;
};

export type AlertFundamentals = {
  businessSummary: string | null;
  country: string | null;
  website: string | null;
  employees: number | null;
  marketCap: string | null;
  trailingPe: number | null;
  forwardPe: number | null;
  priceToSales: number | null;
  priceToBook: number | null;
  revenueGrowth: number | null;
  profitMargins: number | null;
  returnOnEquity: number | null;
  debtToEquity: number | null;
  recommendation: string | null;
  targetMeanPrice: number | null;
  analystCount: number | null;
  beta: number | null;
  fiftyTwoWeekHigh: number | null;
  fiftyTwoWeekLow: number | null;
};

export type AlertRow = {
  ticker: string;
  rank?: number;
  lane?: string;
  name: string | null;
  sector: string | null;
  industry: string | null;
  price: number | null;
  changePercent: number | null;
  annualVolatility: number | null;
  trend50d: number | null;
  distanceFrom52WeekHigh: number | null;
  distanceFrom52WeekLow: number | null;
  scannerScore?: number | null;
  score: number | null;
  setup: string | null;
  provider: string | null;
  providerNote: string | null;
  fundamentals: AlertFundamentals;
  ridge: Record<string, unknown>;
  flow: Record<string, unknown>;
  auction: Record<string, unknown>;
  raw: Record<string, unknown>;
};

export type AlertItem = {
  id: string;
  ticker: string;
  rank: number | null;
  lane: string;
  score: number | null;
  severity: 'High' | 'Medium' | 'Info' | string;
  category: string;
  title: string;
  message: string;
  action: string;
};

export type AlertDigest = {
  generatedAt: string | null;
  headline: string;
  summary: string;
  severityCounts: Record<string, number>;
  categoryCounts: Record<string, number>;
  laneCounts: Record<string, number>;
  priorityTickers: string[];
  riskTickers: string[];
  flowShiftTickers: string[];
  nextSteps: string[];
};

export type WatchlistAlertsResponse = {
  status: 'fresh' | 'partial';
  rows: AlertRow[];
  alerts: AlertItem[];
  digest: AlertDigest;
  provider: string | null;
  providerNote: string | null;
  errors: PerTickerError[];
  meta: Record<string, unknown>;
  watchlist: Watchlist | null;
  /** The backend truncates this field to max_results; watchlist.tickers may be complete. */
  tickers: string[];
};

export type ChartPoint = {
  date: string;
  value?: number | null;
  [key: string]: unknown;
};

export type ChartDataset = {
  chartType: string;
  ticker?: string;
  tickers?: string[];
  period?: string;
  meta: Record<string, unknown>;
  levels: Record<string, unknown>;
  series: Record<string, unknown>;
  rows: Record<string, unknown>[];
  raw: Record<string, unknown>;
};

export type AuctionRequest = {
  ticker?: string;
  tickers?: string[];
  watchlistUrl?: string;
  period?: string;
  maxResults?: number;
};

export type AuctionResponse = {
  datasets: ChartDataset[];
  provider: string | null;
  providerNote: string | null;
  errors: PerTickerError[];
  meta: Record<string, unknown>;
  status: 'fresh' | 'partial';
};

export type TorqueRequest = { ticker: string };

export type TorqueResponse = ChartDataset & {
  chartType: 'torque';
  ticker: string;
  torque: Record<string, unknown>;
};

export type MoneylineRequest = { ticker: string; expiry?: string };

export type MoneylineRow = {
  strike: number;
  callOpenInterest: number | null;
  putOpenInterest: number | null;
  callLast: number | null;
  putLast: number | null;
  netOpenInterest: number | null;
  putCallRatio: number | null;
  raw: Record<string, unknown>;
};

export type MoneylineResponse = {
  chartType: 'moneyline';
  ticker: string;
  meta: Record<string, unknown>;
  rows: MoneylineRow[];
};

export type AgentRole = 'user' | 'assistant';
export type AgentMessage = { role: AgentRole; content: string };

export type AgentChatRequest = {
  messages: AgentMessage[];
  context?: string;
};

export type AgentToolCallSummary = {
  name: string;
  ok: boolean | null;
  durationMs: number | null;
  error: string | null;
};

export type AgentChatResponse = {
  ok: true;
  model: string;
  tools: string[];
  text: string;
  stopReason: string;
  toolCalls: AgentToolCallSummary[];
  toolTrace: string[];
  articles: Record<string, unknown>[];
  /** Artifact metadata only. Binary/base64 fields are discarded at the boundary. */
  artifacts: Record<string, unknown>[];
};

export type AgentStartEvent = { type: 'start'; model: string; tools: string[] };
export type AgentTextEvent = { type: 'text'; text: string };
export type AgentToolCallEvent = {
  type: 'tool_call';
  id: string;
  name: string;
  title?: string;
  group?: string;
  cost?: string;
  input: Record<string, unknown>;
};
export type AgentToolResultEvent = {
  type: 'tool_result';
  id: string;
  name: string;
  ok: boolean;
  status?: number;
  durationMs?: number;
  result?: unknown;
  artifacts: Record<string, unknown>[];
  error?: string;
};
export type AgentArticleEvent = {
  type: 'article';
  article: Record<string, unknown>;
  markdown?: string;
};
export type AgentErrorEvent = { type: 'error'; message: string };
export type AgentDoneEvent = {
  type: 'done';
  stopReason: string;
  text: string;
  toolTrace: string[];
};
export type AgentStreamEvent =
  | AgentStartEvent
  | AgentTextEvent
  | AgentToolCallEvent
  | AgentToolResultEvent
  | AgentArticleEvent
  | AgentErrorEvent
  | AgentDoneEvent;

export type TransportStatus =
  | 'fresh'
  | 'stale-refreshing'
  | 'offline-stale'
  | 'empty-offline'
  | 'partial'
  | 'error'
  | 'streaming'
  | 'cancelled'
  | 'completed';
