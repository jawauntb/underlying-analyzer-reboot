import {
  buildBarPath,
  buildCandlePaths,
  buildLinePath,
  computeChartLayout,
  createLinearScale,
  finiteDomain,
} from '@/src/components/charts/geometry';

function expectRecursivelyFinite(value: unknown): void {
  if (typeof value === 'number') {
    expect(Number.isFinite(value)).toBe(true);
    return;
  }
  if (typeof value === 'string') {
    expect(value).not.toMatch(/NaN|Infinity/);
    return;
  }
  if (Array.isArray(value)) {
    value.forEach(expectRecursivelyFinite);
    return;
  }
  if (value && typeof value === 'object') {
    Object.values(value).forEach(expectRecursivelyFinite);
  }
}

describe('chart geometry', () => {
  it.each([
    ['empty', [], { min: 0, max: 1 }],
    ['one point', [4], { min: 3.8, max: 4.2 }],
    ['flat zero', [0, 0], { min: -1, max: 1 }],
    ['flat nonzero', [10, 10], { min: 9.5, max: 10.5 }],
    ['all negative', [-8, -3, -5], { min: -8, max: -3 }],
    ['nonfinite and null', [null, Number.NaN, Number.POSITIVE_INFINITY, 2], { min: 1.9, max: 2.1 }],
  ])('creates a finite domain for %s values', (_name, values, expected) => {
    const domain = finiteDomain(values);

    expect(domain.min).toBeCloseTo(expected.min);
    expect(domain.max).toBeCloseTo(expected.max);
    expectRecursivelyFinite(domain);
  });

  it('creates finite clamped scales for zero-width and reversed ranges', () => {
    const zeroWidth = createLinearScale({ min: 2, max: 2 }, { min: 30, max: 30 });
    const reversed = createLinearScale({ min: -10, max: 0 }, { min: 100, max: 0 });

    expect(zeroWidth(Number.NaN)).toBe(30);
    expect(zeroWidth(2)).toBe(30);
    expect(reversed(-20)).toBe(100);
    expect(reversed(10)).toBe(0);
    expectRecursivelyFinite([zeroWidth(2), reversed(-5)]);
  });

  it('returns a finite zero-width plot layout', () => {
    const result = computeChartLayout(0, 220, 1, 5);

    expect(result.plot.width).toBe(0);
    expectRecursivelyFinite(result);
  });

  it('builds finite paths while dropping invalid points and missing averages', () => {
    const line = buildLinePath([
      { x: 0, y: 2 },
      { x: Number.NaN, y: 3 },
      { x: 4, y: Number.POSITIVE_INFINITY },
      { x: 6, y: -2 },
    ]);
    const bars = buildBarPath([
      { x: 2, y: 4, baseline: 10, width: 3 },
      { x: 5, y: Number.NaN, baseline: 10, width: 3 },
    ]);

    expect(line).toBe('M0 2M6 -2');
    expect(bars).toContain('M0.5 10');
    expectRecursivelyFinite({ line, bars });
  });

  it('aggregates candle marks into bounded semantic paths without nonfinite output', () => {
    const paths = buildCandlePaths([
      { x: 10, open: 4, high: 2, low: 8, close: 3, width: 5 },
      { x: 20, open: 3, high: 1, low: 9, close: 7, width: 5 },
      { x: 30, open: 4, high: Number.NaN, low: 8, close: 5, width: 5 },
    ]);

    expect(paths.up).toContain('M7.5 4');
    expect(paths.down).toContain('M17.5 3');
    expect(paths.nodeCount).toBe(2);
    expectRecursivelyFinite(paths);
  });

  it.each([
    [320, 1, true, 3],
    [375, 1, false, 5],
    [430, 1, false, 5],
    [430, 1.3, true, 3],
  ])('returns responsive layout at width %i and font scale %s', (width, fontScale, compact, maxLabels) => {
    const result = computeChartLayout(width, 220, fontScale, 12);

    expect(result.compact).toBe(compact);
    expect(result.xLabelIndices.length).toBeLessThanOrEqual(maxLabels);
    expect(result.plot.width).toBeGreaterThanOrEqual(0);
    expect(result.plot.height).toBeGreaterThan(0);
    expectRecursivelyFinite(result);
  });
});
