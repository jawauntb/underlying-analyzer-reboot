import { isRecord } from '@/src/api/guards';

import type { OhlcvPoint } from './decimate';

export type NormalizedResult<T> = {
  data: T;
  droppedPointCount: number;
  warnings: string[];
};

export type AuctionPoint = OhlcvPoint & { categoryIndex: number };
export type LinePoint = { date: string; categoryIndex: number; value: number };
export type FundamentalPoint = { label: string; value: number };
export type MoneylinePoint = {
  strike: number;
  callOpenInterest: number;
  putOpenInterest: number;
  callLast: number | null;
  putLast: number | null;
  netOpenInterest: number | null;
  putCallRatio: number | null;
};

type AuctionModel = NormalizedResult<AuctionPoint[]> & {
  levels: { vah: number | null; val: number | null; poc: number | null };
};

export type TorqueData = {
  priceLines: { close: LinePoint[]; ema75: LinePoint[]; sma50: LinePoint[]; sma200: LinePoint[] };
  categories: string[];
  fundamentals: {
    revenue: FundamentalPoint[];
    grossMargin: FundamentalPoint[];
    operatingMargin: FundamentalPoint[];
  };
  hasTechnicalData: boolean;
  technicalOnly: boolean;
};

export type TorqueModel = TorqueData & {
  data: TorqueData;
  droppedPointCount: number;
  warnings: string[];
};

export type MoneylineModel = NormalizedResult<MoneylinePoint[]> & {
  currentPrice: number | null;
  positioningAvailable: boolean;
};

function array(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function finiteNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function nonemptyString(value: unknown): string | null {
  return typeof value === 'string' && value.length > 0 ? value : null;
}

function childRecord(parent: Record<string, unknown>, key: string): Record<string, unknown> {
  return isRecord(parent[key]) ? parent[key] : {};
}

function levelValue(
  levels: Record<string, unknown>,
  meta: Record<string, unknown>,
  key: 'vah' | 'val' | 'poc',
): number | null {
  return finiteNumber(levels[key]) ?? finiteNumber(meta[key]);
}

export function normalizeAuctionChart(value: unknown): AuctionModel {
  const payload = isRecord(value) ? value : {};
  const series = childRecord(payload, 'series');
  const source = array(series.ohlcv);
  let droppedPointCount = 0;
  const data = source.flatMap((candidate) => {
    if (!isRecord(candidate)) {
      droppedPointCount += 1;
      return [];
    }
    const date = nonemptyString(candidate.date);
    const open = finiteNumber(candidate.open);
    const high = finiteNumber(candidate.high);
    const low = finiteNumber(candidate.low);
    const close = finiteNumber(candidate.close);
    const volume = finiteNumber(candidate.volume);
    if (!date || open === null || high === null || low === null || close === null || volume === null || volume < 0) {
      droppedPointCount += 1;
      return [];
    }
    return [{ date, open, high, low, close, volume, categoryIndex: 0 }];
  });
  data.forEach((point, categoryIndex) => {
    point.categoryIndex = categoryIndex;
  });
  const levels = childRecord(payload, 'levels');
  const meta = childRecord(payload, 'meta');
  const warnings = droppedPointCount ? [`${droppedPointCount} auction points were dropped.`] : [];

  return {
    data,
    levels: {
      vah: levelValue(levels, meta, 'vah'),
      val: levelValue(levels, meta, 'val'),
      poc: levelValue(levels, meta, 'poc'),
    },
    droppedPointCount,
    warnings,
  };
}

function normalizeLine(value: unknown): { points: Omit<LinePoint, 'categoryIndex'>[]; dropped: number } {
  let dropped = 0;
  const points = array(value).flatMap((candidate) => {
    if (!isRecord(candidate)) {
      dropped += 1;
      return [];
    }
    const date = nonemptyString(candidate.date);
    const numericValue = finiteNumber(candidate.value);
    if (!date || numericValue === null) {
      dropped += 1;
      return [];
    }
    return [{ date, value: numericValue }];
  });
  return { points, dropped };
}

function normalizeFundamental(value: unknown): { points: FundamentalPoint[]; dropped: number } {
  let dropped = 0;
  const points = array(value).flatMap((candidate) => {
    if (!isRecord(candidate)) {
      dropped += 1;
      return [];
    }
    const label = nonemptyString(candidate.label);
    const numericValue = finiteNumber(candidate.value);
    if (!label || numericValue === null) {
      dropped += 1;
      return [];
    }
    return [{ label, value: numericValue }];
  });
  return { points, dropped };
}

function labelSignature(points: readonly FundamentalPoint[]): string {
  return points.map((point) => point.label).join('\u0000');
}

export function normalizeTorqueChart(value: unknown): TorqueModel {
  const payload = isRecord(value) ? value : {};
  const series = childRecord(payload, 'series');
  const price = childRecord(series, 'price');
  const fundamentals = childRecord(series, 'fundamentals');
  const close = normalizeLine(price.close);
  const ema75 = normalizeLine(price.ema75);
  const sma50 = normalizeLine(price.sma50);
  const sma200 = normalizeLine(price.sma200);
  const revenue = normalizeFundamental(fundamentals.revenue);
  const grossMargin = normalizeFundamental(fundamentals.gross_margin ?? fundamentals.grossMargin);
  const operatingMargin = normalizeFundamental(fundamentals.operating_margin ?? fundamentals.operatingMargin);

  const rawLines = { close, ema75, sma50, sma200 };
  const categories: string[] = [];
  const seen = new Set<string>();
  Object.values(rawLines).forEach(({ points }) => {
    points.forEach(({ date }) => {
      if (!seen.has(date)) {
        seen.add(date);
        categories.push(date);
      }
    });
  });
  const categoryIndices = new Map(categories.map((date, index) => [date, index]));
  const priceLines = Object.fromEntries(
    Object.entries(rawLines).map(([key, line]) => [
      key,
      line.points.map((point) => ({ ...point, categoryIndex: categoryIndices.get(point.date) ?? 0 })),
    ]),
  ) as TorqueModel['priceLines'];

  const normalizedFundamentals = {
    revenue: revenue.points,
    grossMargin: grossMargin.points,
    operatingMargin: operatingMargin.points,
  };
  const fundamentalSeries = Object.values(normalizedFundamentals).filter((points) => points.length > 0);
  const technicalOnly = fundamentalSeries.length === 0;
  const signatures = new Set(fundamentalSeries.map(labelSignature));
  const droppedPointCount = Object.values(rawLines).reduce((sum, line) => sum + line.dropped, 0) +
    revenue.dropped + grossMargin.dropped + operatingMargin.dropped;
  const warnings: string[] = [];
  if (technicalOnly) warnings.push('Fundamental data unavailable — technicals only.');
  if (fundamentalSeries.length > 1 && signatures.size > 1) {
    warnings.push('Fundamental periods do not fully align.');
  }
  if (droppedPointCount) warnings.push(`${droppedPointCount} torque points were dropped.`);

  const data: TorqueData = {
    priceLines,
    categories,
    fundamentals: normalizedFundamentals,
    hasTechnicalData: Object.values(priceLines).some((line) => line.length > 0),
    technicalOnly,
  };

  return {
    ...data,
    data,
    droppedPointCount,
    warnings,
  };
}

function moneylineSource(payload: Record<string, unknown>): unknown[] {
  const series = childRecord(payload, 'series');
  if (array(series.strikes).length) return array(series.strikes);
  if (array(payload.rows).length) return array(payload.rows);
  return array(childRecord(payload, 'meta').rows);
}

function keyed(candidate: Record<string, unknown>, camel: string, snake: string): unknown {
  return candidate[camel] ?? candidate[snake];
}

export function normalizeMoneylineChart(value: unknown): MoneylineModel {
  const payload = isRecord(value) ? value : {};
  let droppedPointCount = 0;
  const data = moneylineSource(payload).flatMap((candidate) => {
    if (!isRecord(candidate)) {
      droppedPointCount += 1;
      return [];
    }
    const strike = finiteNumber(candidate.strike);
    const callOpenInterest = finiteNumber(keyed(candidate, 'callOpenInterest', 'call_open_interest'));
    const putOpenInterest = finiteNumber(keyed(candidate, 'putOpenInterest', 'put_open_interest'));
    if (
      strike === null || callOpenInterest === null || putOpenInterest === null ||
      callOpenInterest < 0 || putOpenInterest < 0
    ) {
      droppedPointCount += 1;
      return [];
    }
    const suppliedRatio = finiteNumber(keyed(candidate, 'putCallRatio', 'put_call_ratio'));
    return [{
      strike,
      callOpenInterest,
      putOpenInterest,
      callLast: finiteNumber(keyed(candidate, 'callLast', 'call_last')),
      putLast: finiteNumber(keyed(candidate, 'putLast', 'put_last')),
      netOpenInterest: finiteNumber(keyed(candidate, 'netOpenInterest', 'net_open_interest')),
      putCallRatio: callOpenInterest === 0 ? null : suppliedRatio ?? putOpenInterest / callOpenInterest,
    }];
  }).sort((left, right) => left.strike - right.strike);
  const positioningAvailable = data.some(
    (point) => point.callOpenInterest > 0 || point.putOpenInterest > 0,
  );
  const warnings: string[] = [];
  if (!positioningAvailable) warnings.push('Options positioning is unavailable.');
  if (droppedPointCount) warnings.push(`${droppedPointCount} moneyline points were dropped.`);
  const meta = childRecord(payload, 'meta');

  return {
    data,
    currentPrice: finiteNumber(meta.current_price ?? meta.currentPrice),
    positioningAvailable,
    droppedPointCount,
    warnings,
  };
}
