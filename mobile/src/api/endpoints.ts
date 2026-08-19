export const DEFAULT_API_BASE_URL = 'https://underlying-terminal-production.up.railway.app';

export const API_ENDPOINTS = {
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
} as const;

export type ApiConfig =
  | { status: 'configured'; baseUrl: string }
  | { status: 'missing' | 'invalid'; baseUrl: null; message: string };

type ApiConfigOptions = { defaultBaseUrl?: string | null };

export function buildApiConfig(
  override = process.env.EXPO_PUBLIC_API_BASE_URL,
  options: ApiConfigOptions = {},
): ApiConfig {
  const defaultBaseUrl = options.defaultBaseUrl === undefined ? DEFAULT_API_BASE_URL : options.defaultBaseUrl;
  const candidate = override?.trim() || defaultBaseUrl?.trim() || '';
  if (!candidate) {
    return {
      status: 'missing',
      baseUrl: null,
      message: 'Set EXPO_PUBLIC_API_BASE_URL to the public HTTPS API origin.',
    };
  }

  try {
    const parsed = new URL(candidate);
    if (parsed.protocol !== 'https:' || parsed.username || parsed.password) {
      throw new Error('not a public HTTPS origin');
    }
    parsed.hash = '';
    parsed.search = '';
    return { status: 'configured', baseUrl: parsed.toString().replace(/\/+$/, '') };
  } catch {
    return {
      status: 'invalid',
      baseUrl: null,
      message: 'EXPO_PUBLIC_API_BASE_URL must be a credential-free HTTPS URL.',
    };
  }
}

const SYMBOL_PATTERN = /^(?:[A-Z0-9][A-Z0-9.-]{0,14}|\^[A-Z0-9][A-Z0-9.-]{0,13})$/;
const SEARCH_SYMBOL_PATTERN = /^[A-Z0-9.^=-]{1,32}$/;

export function normalizeSymbol(value: string): string {
  const symbol = value.trim().toUpperCase();
  if (!SYMBOL_PATTERN.test(symbol)) {
    throw new Error('Symbol must be 1-15 letters, digits, dots, or hyphens, with at most one leading caret.');
  }
  return symbol;
}

export function encodeSymbol(value: string): string {
  return encodeURIComponent(normalizeSymbol(value));
}

export function normalizeSearchSymbol(value: string): string {
  const symbol = value.trim().toUpperCase();
  if (!SEARCH_SYMBOL_PATTERN.test(symbol)) {
    throw new Error('Search result symbol contains unsupported characters.');
  }
  return symbol;
}

export function normalizeSymbols(values: readonly string[]): string[] {
  const seen = new Set<string>();
  return values.reduce<string[]>((symbols, value) => {
    const symbol = normalizeSymbol(value);
    if (!seen.has(symbol)) {
      seen.add(symbol);
      symbols.push(symbol);
    }
    return symbols;
  }, []);
}

export function endpointUrl(baseUrl: string, endpoint: string, query?: Record<string, string>): string {
  const url = new URL(endpoint, `${baseUrl.replace(/\/+$/, '')}/`);
  Object.entries(query ?? {}).forEach(([key, value]) => url.searchParams.set(key, value));
  return url.toString();
}
