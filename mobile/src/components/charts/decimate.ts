export type OhlcvPoint = {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
};

export type AggregatedOhlcvPoint = OhlcvPoint & {
  endDate: string;
  sourceStartIndex: number;
  sourceEndIndex: number;
};

type Indexed<T> = { item: T; sourceIndex: number; value: number };

export function minMaxDecimate<T>(
  points: readonly T[],
  requestedBudget: number,
  valueOf: (point: T) => number,
): T[] {
  const finitePoints: Indexed<T>[] = points.flatMap((item, sourceIndex) => {
    const value = valueOf(item);
    return Number.isFinite(value) ? [{ item, sourceIndex, value }] : [];
  });
  const budget = Math.max(0, Math.floor(requestedBudget));
  if (budget === 0 || !finitePoints.length) return [];
  if (finitePoints.length <= budget) return finitePoints.map(({ item }) => item);
  if (budget === 1) return [finitePoints[0].item];

  const minimum = finitePoints.reduce((best, point) => (point.value < best.value ? point : best));
  const maximum = finitePoints.reduce((best, point) => (point.value > best.value ? point : best));
  const mandatory = new Map<number, Indexed<T>>();
  [finitePoints[0], minimum, maximum, finitePoints.at(-1)!].forEach((point) => {
    mandatory.set(point.sourceIndex, point);
  });

  if (mandatory.size >= budget) {
    const endpoints = [finitePoints[0], finitePoints.at(-1)!];
    const extrema = [minimum, maximum].filter(
      (point) => !endpoints.some((endpoint) => endpoint.sourceIndex === point.sourceIndex),
    );
    return [...endpoints, ...extrema]
      .slice(0, budget)
      .sort((left, right) => left.sourceIndex - right.sourceIndex)
      .map(({ item }) => item);
  }

  const selected = new Map(mandatory);
  const candidates = finitePoints.filter((point) => !selected.has(point.sourceIndex));
  const capacity = budget - selected.size;
  const bucketCount = Math.max(1, Math.ceil(capacity / 2));
  const bucketSize = Math.max(1, Math.ceil(candidates.length / bucketCount));

  for (let start = 0; start < candidates.length && selected.size < budget; start += bucketSize) {
    const bucket = candidates.slice(start, start + bucketSize);
    const low = bucket.reduce((best, point) => (point.value < best.value ? point : best));
    const high = bucket.reduce((best, point) => (point.value > best.value ? point : best));
    [low, high]
      .sort((left, right) => left.sourceIndex - right.sourceIndex)
      .forEach((point) => {
        if (selected.size < budget) selected.set(point.sourceIndex, point);
      });
  }

  if (selected.size < budget) {
    for (const candidate of candidates) {
      if (selected.size >= budget) break;
      selected.set(candidate.sourceIndex, candidate);
    }
  }

  return [...selected.values()]
    .sort((left, right) => left.sourceIndex - right.sourceIndex)
    .map(({ item }) => item);
}

function validOhlcv(point: OhlcvPoint): boolean {
  return Boolean(point.date) &&
    [point.open, point.high, point.low, point.close, point.volume].every(Number.isFinite);
}

export function aggregateOhlcv(
  points: readonly OhlcvPoint[],
  requestedBudget: number,
): AggregatedOhlcvPoint[] {
  const valid = points.flatMap((point, sourceIndex) =>
    validOhlcv(point) ? [{ point, sourceIndex }] : [],
  );
  const budget = Math.max(0, Math.floor(requestedBudget));
  if (!valid.length || budget === 0) return [];
  const bucketSize = Math.max(1, Math.ceil(valid.length / budget));
  const aggregated: AggregatedOhlcvPoint[] = [];

  for (let start = 0; start < valid.length; start += bucketSize) {
    const bucket = valid.slice(start, start + bucketSize);
    const first = bucket[0];
    const last = bucket.at(-1)!;
    aggregated.push({
      date: first.point.date,
      endDate: last.point.date,
      open: first.point.open,
      high: Math.max(...bucket.map(({ point }) => point.high)),
      low: Math.min(...bucket.map(({ point }) => point.low)),
      close: last.point.close,
      volume: bucket.reduce((sum, { point }) => sum + point.volume, 0),
      sourceStartIndex: first.sourceIndex,
      sourceEndIndex: last.sourceIndex,
    });
  }

  return aggregated;
}
