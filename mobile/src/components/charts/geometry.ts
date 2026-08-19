export type NumericDomain = { min: number; max: number };
export type ChartPoint = { x: number; y: number };

type Range = { min: number; max: number };
type BarMark = { x: number; y: number; baseline: number; width: number };
type CandleMark = {
  x: number;
  open: number;
  high: number;
  low: number;
  close: number;
  width: number;
};

export type ChartLayout = {
  width: number;
  height: number;
  compact: boolean;
  plot: { left: number; top: number; width: number; height: number; right: number; bottom: number };
  xLabelIndices: number[];
};

function finite(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

function pathNumber(value: number): string {
  const rounded = Math.round(value * 1000) / 1000;
  return String(Object.is(rounded, -0) ? 0 : rounded);
}

export function finiteDomain(values: readonly unknown[]): NumericDomain {
  const numbers = values.filter(finite);
  if (!numbers.length) return { min: 0, max: 1 };

  const min = Math.min(...numbers);
  const max = Math.max(...numbers);
  if (min !== max) return { min, max };

  const padding = min === 0 ? 1 : Math.abs(min) * 0.05;
  return { min: min - padding, max: max + padding };
}

export function createLinearScale(domain: NumericDomain, range: Range): (value: unknown) => number {
  const rangeMin = finite(range.min) ? range.min : 0;
  const rangeMax = finite(range.max) ? range.max : rangeMin;
  const domainMin = finite(domain.min) ? domain.min : 0;
  const domainMax = finite(domain.max) ? domain.max : domainMin + 1;
  const domainSpan = domainMax - domainMin;
  const low = Math.min(domainMin, domainMax);
  const high = Math.max(domainMin, domainMax);

  return (value: unknown) => {
    if (!finite(value) || !finite(domainSpan) || domainSpan === 0 || rangeMin === rangeMax) {
      return rangeMin;
    }
    const clamped = Math.min(high, Math.max(low, value));
    const scaled = rangeMin + ((clamped - domainMin) / domainSpan) * (rangeMax - rangeMin);
    return finite(scaled) ? scaled : rangeMin;
  };
}

export function buildLinePath(points: readonly ChartPoint[]): string {
  let penDown = false;
  let path = '';
  for (const point of points) {
    if (!finite(point.x) || !finite(point.y)) {
      penDown = false;
      continue;
    }
    path += `${penDown ? 'L' : 'M'}${pathNumber(point.x)} ${pathNumber(point.y)}`;
    penDown = true;
  }
  return path;
}

export function buildBarPath(marks: readonly BarMark[]): string {
  return marks
    .filter((mark) => [mark.x, mark.y, mark.baseline, mark.width].every(finite))
    .map((mark) => {
      const half = Math.max(0, mark.width) / 2;
      const left = mark.x - half;
      const right = mark.x + half;
      return `M${pathNumber(left)} ${pathNumber(mark.baseline)}L${pathNumber(left)} ${pathNumber(mark.y)}L${pathNumber(right)} ${pathNumber(mark.y)}L${pathNumber(right)} ${pathNumber(mark.baseline)}Z`;
    })
    .join('');
}

function candleSubpath(mark: CandleMark): string {
  const half = Math.max(0.5, mark.width / 2);
  const bodyTop = Math.min(mark.open, mark.close);
  const bodyBottom = Math.max(mark.open, mark.close);
  const visibleBottom = bodyBottom === bodyTop ? bodyBottom + 0.75 : bodyBottom;
  return `M${pathNumber(mark.x - half)} ${pathNumber(mark.open)}L${pathNumber(mark.x + half)} ${pathNumber(mark.open)}L${pathNumber(mark.x + half)} ${pathNumber(mark.close)}L${pathNumber(mark.x - half)} ${pathNumber(mark.close)}ZM${pathNumber(mark.x)} ${pathNumber(mark.high)}L${pathNumber(mark.x)} ${pathNumber(bodyTop)}M${pathNumber(mark.x)} ${pathNumber(visibleBottom)}L${pathNumber(mark.x)} ${pathNumber(mark.low)}`;
}

export function buildCandlePaths(marks: readonly CandleMark[]): {
  up: string;
  down: string;
  nodeCount: number;
} {
  const valid = marks.filter((mark) =>
    [mark.x, mark.open, mark.high, mark.low, mark.close, mark.width].every(finite),
  );
  const up = valid.filter((mark) => mark.close <= mark.open).map(candleSubpath).join('');
  const down = valid.filter((mark) => mark.close > mark.open).map(candleSubpath).join('');
  return { up, down, nodeCount: Number(Boolean(up)) + Number(Boolean(down)) };
}

function labelIndices(pointCount: number, maximum: number): number[] {
  if (pointCount <= 0) return [];
  if (pointCount <= maximum) return Array.from({ length: pointCount }, (_, index) => index);
  const indices = new Set<number>([0, pointCount - 1]);
  for (let step = 1; step < maximum - 1; step += 1) {
    indices.add(Math.round((step * (pointCount - 1)) / (maximum - 1)));
  }
  return [...indices].sort((left, right) => left - right);
}

export function computeChartLayout(
  requestedWidth: number,
  requestedHeight: number,
  requestedFontScale: number,
  pointCount: number,
): ChartLayout {
  const width = finite(requestedWidth) ? Math.max(0, requestedWidth) : 0;
  const height = finite(requestedHeight) ? Math.max(120, requestedHeight) : 220;
  const fontScale = finite(requestedFontScale) ? Math.max(1, requestedFontScale) : 1;
  const compact = width < 350 || fontScale >= 1.3;
  const horizontalInset = compact ? 12 : 16;
  const top = 10;
  const bottomInset = compact ? 34 : 30;
  const plotWidth = Math.max(0, width - horizontalInset * 2);
  const plotHeight = Math.max(1, height - top - bottomInset);

  return {
    width,
    height,
    compact,
    plot: {
      left: horizontalInset,
      top,
      width: plotWidth,
      height: plotHeight,
      right: horizontalInset + plotWidth,
      bottom: top + plotHeight,
    },
    xLabelIndices: labelIndices(Math.max(0, Math.floor(pointCount)), compact ? 3 : 5),
  };
}
