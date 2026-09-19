import {
  normalizeAuctionChart,
  normalizeMoneylineChart,
  normalizePeerForecastChart,
  normalizeTorqueChart,
} from '@/src/components/charts/models';

describe('auction chart model', () => {
  it('uses series.ohlcv in trading-session order and prefers levels over meta', () => {
    const result = normalizeAuctionChart({
      meta: { vah: 999, val: 1, poc: 2 },
      levels: { vah: 14, val: 8, poc: 11 },
      series: {
        ohlcv: [
          { date: '2026-08-14', open: 10, high: 12, low: 9, close: 11, volume: 2 },
          { date: '2026-08-17', open: 11, high: 14, low: 8, close: 13, volume: 3 },
        ],
      },
    });

    expect(result.data.map((point) => [point.date, point.categoryIndex])).toEqual([
      ['2026-08-14', 0],
      ['2026-08-17', 1],
    ]);
    expect(result.levels).toEqual({ vah: 14, val: 8, poc: 11 });
    expect(result.droppedPointCount).toBe(0);
  });

  it('drops malformed candles independently and never invents missing levels', () => {
    const result = normalizeAuctionChart({
      meta: { vah: Number.POSITIVE_INFINITY },
      series: {
        ohlcv: [
          { date: 'ok', open: 0, high: 0, low: 0, close: 0, volume: 0 },
          { date: 'bad', open: 1, high: Number.NaN, low: 0, close: 1, volume: 1 },
          null,
        ],
      },
    });

    expect(result.data).toHaveLength(1);
    expect(result.levels).toEqual({ vah: null, val: null, poc: null });
    expect(result.droppedPointCount).toBe(2);
    expect(result.warnings).toContain('2 auction points were dropped.');
  });
});

describe('torque chart model', () => {
  it('accepts empty price and reports a technical-only state when fundamentals are unavailable', () => {
    const result = normalizeTorqueChart({
      meta: { fundamental_data_available: true },
      series: { price: {}, fundamentals: {} },
    });

    expect(result.priceLines.close).toEqual([]);
    expect(result.data.priceLines.close).toEqual([]);
    expect(result.technicalOnly).toBe(true);
    expect(result.warnings).toContain('Fundamental data unavailable — technicals only.');
  });

  it('guards close, EMA, SMA, and fundamental arrays independently', () => {
    const result = normalizeTorqueChart({
      series: {
        price: {
          close: [{ date: 'd1', value: 10 }, { date: 'd2', value: Number.NaN }],
          ema75: [{ date: 'd1', value: 9 }, { date: 'd2', value: 11 }],
          sma50: 'missing',
          sma200: [{ date: 'd2', value: 8 }],
        },
        fundamentals: {
          revenue: [{ label: 'Q1', value: 100 }, { label: 'Q2', value: Number.POSITIVE_INFINITY }],
          gross_margin: [{ label: 'Q2', value: 40 }],
          operating_margin: [{ label: 'Q1', value: 10 }],
        },
      },
    });

    expect(result.priceLines.close.map((point) => point.value)).toEqual([10]);
    expect(result.priceLines.ema75.map((point) => point.value)).toEqual([9, 11]);
    expect(result.priceLines.sma50).toEqual([]);
    expect(result.priceLines.sma200.map((point) => point.value)).toEqual([8]);
    expect(result.categories).toEqual(['d1', 'd2']);
    expect(result.fundamentals.revenue).toEqual([{ label: 'Q1', value: 100 }]);
    expect(result.technicalOnly).toBe(false);
    expect(result.warnings).toContain('Fundamental periods do not fully align.');
    expect(result.droppedPointCount).toBe(2);
  });

  it('keeps overlays when the base price line is absent', () => {
    const result = normalizeTorqueChart({
      series: { price: { close: [], ema75: [{ date: 'd1', value: -4 }] } },
    });

    expect(result.priceLines.close).toEqual([]);
    expect(result.priceLines.ema75).toHaveLength(1);
    expect(result.hasTechnicalData).toBe(true);
  });
});

describe('moneyline chart model', () => {
  it('consumes series.strikes, sorts strikes, rejects negative sides, and permits negative net OI', () => {
    const result = normalizeMoneylineChart({
      meta: { current_price: 101 },
      series: {
        strikes: [
          { strike: 105, call_open_interest: 0, put_open_interest: 4, net_open_interest: -4, put_call_ratio: 3 },
          { strike: 95, call_open_interest: 3, put_open_interest: 1, net_open_interest: 2, put_call_ratio: 1 / 3 },
          { strike: 100, call_open_interest: -1, put_open_interest: 2, net_open_interest: -3 },
        ],
      },
    });

    expect(result.data.map((row) => row.strike)).toEqual([95, 105]);
    expect(result.data[1]).toMatchObject({ netOpenInterest: -4, putCallRatio: null });
    expect(result.droppedPointCount).toBe(1);
    expect(result.currentPrice).toBe(101);
  });

  it('accepts normalized rows and represents all-zero positioning as unavailable', () => {
    const result = normalizeMoneylineChart({
      rows: [
        { strike: 100, callOpenInterest: 0, putOpenInterest: 0, netOpenInterest: 0, putCallRatio: 0 },
      ],
    });

    expect(result.data[0].putCallRatio).toBeNull();
    expect(result.positioningAvailable).toBe(false);
    expect(result.warnings).toContain('Options positioning is unavailable.');
  });
});

describe('peer forecast chart model', () => {
  const peer = (symbol: string, expected: number, extra: Record<string, unknown> = {}) => ({
    symbol,
    bucket: expected >= 0 ? 'over' : 'under',
    expected_excess_return: expected,
    probabilities: { strong_under: 0.1, under: 0.1, inline: 0.2, over: 0.3, strong_over: 0.3 },
    confidence: 0.3,
    realized_excess_return_last: expected / 2,
    predicted_excess_return_last: expected / 3,
    ...extra,
  });

  it('ranks peers by expected excess return, flags the focus ticker, and pairs the backtest', () => {
    const result = normalizePeerForecastChart({
      ticker: 'nvda',
      sector_etf: 'XLK',
      horizon_months: 3,
      bucket: 'over',
      confidence: 0.72,
      confidence_floor: 0.55,
      expected_excess_return: 0.031,
      last_labeled_date: '2026-05-31',
      peers: [peer('AAPL', 0.01), peer('NVDA', 0.031), peer('AMD', -0.02)],
    });

    expect(result.ranked.map((bar) => [bar.rank, bar.symbol, bar.isFocus])).toEqual([
      [1, 'NVDA', true],
      [2, 'AAPL', false],
      [3, 'AMD', false],
    ]);
    expect(result.pairs.map((pair) => pair.symbol)).toEqual(['AAPL', 'NVDA', 'AMD']);
    expect(result.statesBucket).toBe(true);
    expect(result.sectorEtf).toBe('XLK');
    expect(result.horizonMonths).toBe(3);
    expect(result.lastLabeledDate).toBe('2026-05-31');
    expect(result.warnings).toEqual([]);
  });

  it('reads the /api/data/charts dataset shape and drops malformed rows without inventing pairs', () => {
    const result = normalizePeerForecastChart({
      chart_type: 'peer-forecast',
      meta: { ticker: 'NVDA', bucket: 'inline', confidence: 0.4, confidence_floor: 0.55 },
      series: {
        ranked: [
          { symbol: 'NVDA', bucket: 'inline', expected_excess_return: 0.004, confidence: 0.4 },
          { symbol: 'AAPL', bucket: 'over', expected_excess_return: Number.NaN, confidence: 0.4 },
          'junk',
        ],
        predicted_vs_realized: [],
      },
    });

    expect(result.ranked).toHaveLength(1);
    expect(result.ranked[0].isFocus).toBe(true);
    expect(result.pairs).toEqual([]);
    expect(result.statesBucket).toBe(false);
    expect(result.droppedPointCount).toBe(2);
    expect(result.warnings).toContain('2 peer forecast rows were dropped.');
    expect(result.warnings).toContain('No labelled cross-section to compare against yet.');
  });

  it('reports an unavailable model for an empty payload', () => {
    const result = normalizePeerForecastChart(null);
    expect(result.ranked).toEqual([]);
    expect(result.warnings).toEqual(['Peer forecast is unavailable.']);
  });
});
