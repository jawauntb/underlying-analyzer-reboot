export const MOBILE_AGENT_TOOLS = [
  'analyze_ticker',
  'stock_fax',
  'sec_source_pack',
  'search_news',
  'chart_data',
  'provider_status',
] as const;

export function exactMobileToolEcho(value: unknown): string[] | null {
  if (
    !Array.isArray(value)
    || value.length !== MOBILE_AGENT_TOOLS.length
    || value.some((item) => typeof item !== 'string')
  ) {
    return null;
  }
  const actual = new Set<string>(value);
  if (
    actual.size !== MOBILE_AGENT_TOOLS.length
    || MOBILE_AGENT_TOOLS.some((tool) => !actual.has(tool))
  ) {
    return null;
  }
  return [...value];
}
