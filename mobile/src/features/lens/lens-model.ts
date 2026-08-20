import { normalizeSymbol } from '@/src/api/endpoints';

export const RESEARCH_DEPTHS = ['glance', 'diagnose', 'deep-dive'] as const;
export type ResearchDepth = (typeof RESEARCH_DEPTHS)[number];

export const LENS_AUCTION_PERIODS = ['5d', '1mo', '3mo', '6mo', '1y'] as const;
export type LensAuctionPeriod = (typeof LENS_AUCTION_PERIODS)[number];

export const CHART_INTERVALS = ['15m', '1d', '1w'] as const;
export type ChartInterval = (typeof CHART_INTERVALS)[number];

export const CHART_INTERVAL_CHIPS = [
  { label: '15m', value: '15m', spoken: '15 minute', period: '5d' },
  { label: '1D', value: '1d', spoken: 'daily', period: '3mo' },
  { label: '1W', value: '1w', spoken: 'weekly', period: '1y' },
] as const satisfies readonly {
  label: string;
  value: ChartInterval;
  spoken: string;
  period: LensAuctionPeriod;
}[];

export const RESEARCH_DEPTH_LABELS: Record<ResearchDepth, string> = {
  glance: 'Glance',
  diagnose: 'Diagnose',
  'deep-dive': 'Deep Dive',
};

export const RESEARCH_DEPTH_DESCRIPTIONS: Record<ResearchDepth, string> = {
  glance: 'Glance opens Torque and 5d Auction.',
  diagnose: 'Diagnose adds options positioning through Moneyline.',
  'deep-dive': 'Deep Dive prepares a separate, explicit Research Run.',
};

export function normalizeLensSymbol(value: string): { symbol: string; error: null } | { symbol: null; error: string } {
  try {
    return { symbol: normalizeSymbol(value), error: null };
  } catch {
    return { symbol: null, error: 'Invalid ticker symbol. Return to Lists or Pulse and choose a valid symbol.' };
  }
}

export function moveResearchDepth(depth: ResearchDepth, offset: -1 | 1): ResearchDepth {
  const index = RESEARCH_DEPTHS.indexOf(depth);
  return RESEARCH_DEPTHS[Math.max(0, Math.min(RESEARCH_DEPTHS.length - 1, index + offset))];
}

export function researchDepthAtPosition(locationX: number, width: number): ResearchDepth {
  if (!Number.isFinite(locationX) || !Number.isFinite(width) || width <= 0) return 'glance';
  const index = Math.max(0, Math.min(2, Math.floor((locationX / width) * 3)));
  return RESEARCH_DEPTHS[index];
}
