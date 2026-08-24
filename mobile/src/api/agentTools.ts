export const MOBILE_AGENT_TOOLS = [
  'ticker_research_bundle',
  'analyze_ticker',
  'stock_fax',
  'sec_source_pack',
  'search_news',
  'chart_data',
  'provider_status',
] as const;

/** Tool set used by saved v1 Library records before complete packets shipped. */
export const LEGACY_MOBILE_AGENT_TOOLS = [
  'analyze_ticker',
  'stock_fax',
  'sec_source_pack',
  'search_news',
  'chart_data',
  'provider_status',
] as const;

function exactToolEcho(value: unknown, expected: readonly string[]): string[] | null {
  if (
    !Array.isArray(value)
    || value.length !== expected.length
    || value.some((item) => typeof item !== 'string')
  ) {
    return null;
  }
  const actual = new Set<string>(value);
  if (actual.size !== expected.length || expected.some((tool) => !actual.has(tool))) {
    return null;
  }
  return [...value];
}

export function exactMobileToolEcho(value: unknown): string[] | null {
  return exactToolEcho(value, MOBILE_AGENT_TOOLS);
}

export function exactLegacyMobileToolEcho(value: unknown): string[] | null {
  return exactToolEcho(value, LEGACY_MOBILE_AGENT_TOOLS);
}
