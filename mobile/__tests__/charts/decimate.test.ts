import { aggregateOhlcv, minMaxDecimate } from '@/src/components/charts/decimate';

describe('chart decimation', () => {
  it('preserves first, last, global minimum, global maximum, order, and budget', () => {
    const values = Array.from({ length: 101 }, (_, index) => ({ index, value: Math.sin(index / 4) }));
    values[37].value = -99;
    values[72].value = 120;

    const result = minMaxDecimate(values, 12, (point) => point.value);
    const indices = result.map((point) => point.index);

    expect(result.length).toBeLessThanOrEqual(12);
    expect(indices[0]).toBe(0);
    expect(indices.at(-1)).toBe(100);
    expect(indices).toContain(37);
    expect(indices).toContain(72);
    expect(indices).toEqual([...indices].sort((left, right) => left - right));
  });

  it('drops nonfinite values without losing finite endpoints or exceeding budget', () => {
    const result = minMaxDecimate(
      [
        { index: 0, value: Number.NaN },
        { index: 1, value: 4 },
        { index: 2, value: Number.POSITIVE_INFINITY },
        { index: 3, value: -1 },
      ],
      4,
      (point) => point.value,
    );

    expect(result.map((point) => point.index)).toEqual([1, 3]);
  });

  it('aggregates OHLCV buckets with first open, last close, extrema, and volume sum', () => {
    const result = aggregateOhlcv(
      [
        { date: '2026-08-10', open: 10, high: 12, low: 9, close: 11, volume: 2 },
        { date: '2026-08-11', open: 11, high: 14, low: 8, close: 13, volume: 3 },
        { date: '2026-08-12', open: 13, high: 15, low: 12, close: 14, volume: 5 },
        { date: '2026-08-13', open: 14, high: 16, low: 7, close: 8, volume: 7 },
      ],
      2,
    );

    expect(result).toEqual([
      {
        date: '2026-08-10',
        endDate: '2026-08-11',
        open: 10,
        high: 14,
        low: 8,
        close: 13,
        volume: 5,
        sourceStartIndex: 0,
        sourceEndIndex: 1,
      },
      {
        date: '2026-08-12',
        endDate: '2026-08-13',
        open: 13,
        high: 16,
        low: 7,
        close: 8,
        volume: 12,
        sourceStartIndex: 2,
        sourceEndIndex: 3,
      },
    ]);
  });
});
