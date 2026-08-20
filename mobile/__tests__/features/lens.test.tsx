import { act, fireEvent, render, screen, waitFor } from '@testing-library/react-native';
import { StyleSheet } from 'react-native';

import { generateStaticParams } from '@/app/ticker/[symbol]';
import type { AuctionResponse, OptionsChainResponse, ProviderStatusResponse, WatchlistAlertsResponse } from '@/src/api/contracts';
import { API_ENDPOINTS } from '@/src/api/endpoints';
import LensScreen from '@/src/features/lens/LensScreen';
import { CACHE_SCHEMA_VERSION, type CacheRecord } from '@/src/state/cache';

jest.mock('@expo/vector-icons/Ionicons', () => {
  const React = jest.requireActual('react');
  const { Text } = jest.requireActual('react-native');
  function MockIonicon({ name }: { name: string }) {
    return React.createElement(Text, null, name);
  }
  return MockIonicon;
});

jest.mock('react-native-safe-area-context', () => {
  const React = jest.requireActual('react');
  const { View } = jest.requireActual('react-native');
  return { SafeAreaView: ({ children, ...props }: { children?: React.ReactNode }) => React.createElement(View, props, children) };
});

const torque = {
  chartType: 'torque' as const,
  ticker: 'AAPL',
  meta: {},
  levels: {},
  series: {
    price: { close: [{ date: '2026-08-18', value: 220 }] },
    fundamentals: {},
  },
  rows: [],
  raw: {},
  torque: {},
};

const auction = {
  status: 'fresh' as const,
  datasets: [{
    chartType: 'auction',
    ticker: 'AAPL',
    period: '5d',
    meta: {},
    levels: {},
    series: { ohlcv: [{ date: '2026-08-18', open: 218, high: 222, low: 217, close: 220, volume: 10 }] },
    rows: [],
    raw: {},
  }],
  provider: null,
  providerNote: null,
  errors: [],
  meta: {},
};

const moneyline = {
  chartType: 'moneyline' as const,
  ticker: 'AAPL',
  meta: {},
  rows: [{ strike: 220, callOpenInterest: 10, putOpenInterest: 5, callLast: null, putLast: null, netOpenInterest: 5, putCallRatio: 0.5, raw: {} }],
};

const overview = (overrides: Partial<WatchlistAlertsResponse> = {}): WatchlistAlertsResponse => ({
  status: 'fresh',
  rows: [{
    ticker: 'AAPL',
    rank: 1,
    lane: 'Priority',
    name: 'Apple Inc.',
    sector: 'Technology',
    industry: 'Consumer Electronics',
    price: 231.42,
    changePercent: 1.28,
    annualVolatility: 0.24,
    trend50d: 0.08,
    distanceFrom52WeekHigh: -0.03,
    distanceFrom52WeekLow: 0.41,
    scannerScore: 89,
    score: 92,
    setup: 'Support reclaimed with improving participation.',
    provider: 'Fixture provider',
    providerNote: 'Delayed market snapshot.',
    fundamentals: {
      businessSummary: 'Designs consumer technology and services.',
      country: 'United States',
      website: 'https://apple.com',
      employees: 164_000,
      marketCap: '3.42T',
      trailingPe: 34.2,
      forwardPe: 28.4,
      priceToSales: 8.6,
      priceToBook: 52.1,
      revenueGrowth: 0.052,
      profitMargins: 0.244,
      returnOnEquity: 1.51,
      debtToEquity: 145.2,
      recommendation: 'buy',
      targetMeanPrice: 245.5,
      analystCount: 42,
      beta: 1.18,
      fiftyTwoWeekHigh: 237.49,
      fiftyTwoWeekLow: 164.08,
    },
    ridge: { recommendation: 'BUY', trend_confirmed: true, total_return: 0.18 },
    flow: { state: 'LONG', score: 72, signal: 'Accumulation', fresh_long: true, volume_score: 64 },
    auction: { location: 'above value', poc: 227, vah: 230, val: 224, distance_to_poc: 0.019 },
    raw: {},
  }],
  alerts: [{
    id: 'aapl-fresh-long',
    ticker: 'AAPL',
    rank: 1,
    lane: 'Priority',
    score: 92,
    severity: 'Medium',
    category: 'Flow',
    title: 'Fresh long flow',
    message: 'AAPL printed a fresh long Flow Compass shift.',
    action: 'Compare entry quality against auction value and Ridge state.',
  }],
  digest: {
    generatedAt: '2026-08-19T12:00:00Z',
    headline: 'AAPL setup ready',
    summary: 'One current setup is available.',
    severityCounts: { Medium: 1 },
    categoryCounts: { Flow: 1 },
    laneCounts: { Priority: 1 },
    priorityTickers: ['AAPL'],
    riskTickers: [],
    flowShiftTickers: ['AAPL'],
    nextSteps: [],
  },
  provider: 'Fixture provider',
  providerNote: 'Delayed market snapshot.',
  errors: [],
  meta: {},
  watchlist: null,
  tickers: ['AAPL'],
  ...overrides,
});

const cachedRecord = <T,>(data: T, fetchedAt: number): CacheRecord<T> => ({
  schemaVersion: CACHE_SCHEMA_VERSION,
  data,
  fetchedAt,
  accessedAt: fetchedAt,
});

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
};

function dependencies(options: {
  cached?: CacheRecord<WatchlistAlertsResponse> | null;
  chartCached?: CacheRecord<AuctionResponse> | null;
  live?: Promise<WatchlistAlertsResponse>;
  liveError?: Error;
  reachability?: 'online' | 'offline' | 'unknown';
  now?: number;
  providers?: ProviderStatusResponse;
  optionsChain?: OptionsChainResponse;
} = {}) {
  const cache = {
    read: jest.fn(async (descriptor: { route?: string }) =>
      descriptor.route === API_ENDPOINTS.auction ? options.chartCached ?? null : options.cached ?? null),
    write: jest.fn(async (_descriptor?: { route?: string }) => undefined),
  };
  const client = {
    baseUrl: 'https://api.test',
    watchlistAlerts: jest.fn(() => options.liveError ? Promise.reject(options.liveError) : options.live ?? Promise.resolve(overview())),
    torque: jest.fn(async () => torque),
    auction: jest.fn(async () => auction),
    moneyline: jest.fn(async () => moneyline),
    ...(options.providers ? { providers: jest.fn(async () => options.providers) } : {}),
    ...(options.optionsChain ? { optionsChain: jest.fn(async () => options.optionsChain) } : {}),
  };
  const router = { push: jest.fn() };
  const haptics = { selectionAsync: jest.fn(async () => undefined) };
  return {
    cache,
    client,
    router,
    haptics,
    props: {
      cache: cache as never,
      client: client as never,
      reachability: options.reachability ?? 'online',
      router: router as never,
      haptics,
      symbol: ' aapl ',
      width: 375,
      fontScale: 1,
      now: () => options.now ?? 100_000,
    },
  };
}

describe('LensScreen', () => {
  it('shows Massive freshness status and the richer options pulse only when Diagnose is opened', async () => {
    const deps = dependencies({
      providers: {
        primary: 'massive',
        fallback: 'yfinance + nasdaq',
        massiveConfigured: true,
        fallbackEnabled: true,
        freshness: { stocks: 'realtime', options: 'plan-dependent' },
        streaming: {
          enabled: true,
          configured: true,
          transport: 'SSE backed by Massive WebSocket',
          freshness: 'realtime',
          endpoint: '/api/data/market/stream',
        },
        notes: [],
      },
      optionsChain: {
        ticker: 'AAPL',
        expiry: '2026-09-18',
        currentPrice: 231.42,
        expirations: ['2026-09-18'],
        provider: 'massive',
        providerNote: 'Options Developer',
        rows: [{
          strike: 230,
          callOpenInterest: 100,
          putOpenInterest: 80,
          callLast: 5.2,
          putLast: 4.8,
          callImpliedVolatility: 0.31,
          putImpliedVolatility: 0.29,
          callDelta: 0.58,
          putDelta: -0.42,
          callBid: 5,
          callAsk: 5.4,
          putBid: 4.6,
          putAsk: 5,
          callVolume: 120,
          putVolume: 90,
        }],
      },
    });
    render(<LensScreen {...deps.props} />);

    expect((await screen.findAllByText('realtime')).length).toBeGreaterThanOrEqual(2);
    expect(screen.queryByText('OPTIONS PULSE')).toBeNull();
    fireEvent.press(screen.getByRole('button', { name: 'Select Diagnose' }));
    fireEvent.press(screen.getByRole('button', { name: 'Open Diagnose' }));
    expect(await screen.findByText('OPTIONS PULSE')).toBeTruthy();
    expect(screen.getByText('31.0%')).toBeTruthy();
    expect(screen.getByText('0.58')).toBeTruthy();
    expect(screen.getByText('$5.00 / $5.40')).toBeTruthy();
  });

  it('auto-loads the overview and a visible 3-month price chart while deeper research stays explicit', async () => {
    const deps = dependencies();
    render(<LensScreen {...deps.props} />);

    expect(screen.getByRole('header', { name: 'AAPL' })).toBeTruthy();
    expect(generateStaticParams()).toEqual(expect.arrayContaining([{ symbol: 'AAPL' }]));
    expect(await screen.findByText('Apple Inc.')).toBeTruthy();
    expect(screen.getByText('$231.42')).toBeTruthy();
    expect(screen.getByText('+1.28%')).toBeTruthy();
    expect(screen.getByText('Support reclaimed with improving participation.')).toBeTruthy();
    expect(screen.getByText('Fresh long flow')).toBeTruthy();
    expect(screen.getByText('BUY')).toBeTruthy();
    expect(screen.getByText('LONG · 72.0')).toBeTruthy();
    expect(screen.getByText('Accumulation · Fresh long shift · Volume score 64.0')).toBeTruthy();
    expect(screen.getByText('Above value')).toBeTruthy();
    expect(screen.getByText('Designs consumer technology and services.')).toBeTruthy();
    expect(screen.getByText('3.42T')).toBeTruthy();
    expect(screen.getByText('34.2')).toBeTruthy();
    expect(screen.getByText('28.4')).toBeTruthy();
    expect(screen.getByText('+5.2%')).toBeTruthy();
    expect(screen.getByText('+24.4%')).toBeTruthy();
    expect(screen.getByText('$164.08–$237.49')).toBeTruthy();
    expect(screen.getByText('$245.50')).toBeTruthy();
    expect(screen.getByText('42 analysts · Buy')).toBeTruthy();
    expect(screen.getByText(/Fixture provider · Updated/)).toBeTruthy();
    expect(deps.client.watchlistAlerts).toHaveBeenCalledWith({ ticker: 'AAPL' }, expect.anything());
    expect(deps.client.auction).toHaveBeenCalledWith({ ticker: 'AAPL', period: '3mo', interval: '1d' }, expect.anything());
    expect(await screen.findByRole('button', { name: 'View AAPL 3M Price & value data' })).toBeTruthy();
    expect(screen.getByText(/Selected depth: Glance/)).toBeTruthy();
    expect(screen.getByText(/Opened depth: None/)).toBeTruthy();
    fireEvent.press(screen.getByRole('button', { name: 'Select Diagnose' }));
    expect(screen.getByText(/Selected depth: Diagnose/)).toBeTruthy();
    expect(screen.getByText(/Opened depth: None/)).toBeTruthy();
    expect(deps.client.torque).not.toHaveBeenCalled();
    expect(deps.client.moneyline).not.toHaveBeenCalled();
  });

  it('switches visible chart ranges and ignores a late response from the replaced range', async () => {
    const initial = deferred<typeof auction>();
    const replacement = deferred<typeof auction>();
    const deps = dependencies();
    deps.client.auction
      .mockImplementationOnce(() => initial.promise)
      .mockImplementationOnce(() => replacement.promise);
    render(<LensScreen {...deps.props} />);

    await waitFor(() => expect(deps.client.auction).toHaveBeenCalledWith({ ticker: 'AAPL', period: '3mo', interval: '1d' }, expect.anything()));
    fireEvent.press(screen.getByRole('tab', { name: 'Show 1 year chart' }));
    await waitFor(() => expect(deps.client.auction).toHaveBeenCalledWith({ ticker: 'AAPL', period: '1y', interval: '1d' }, expect.anything()));
    await act(async () => replacement.resolve({
      ...auction,
      datasets: [{ ...auction.datasets[0], period: '1y', series: { ohlcv: [{ date: '2026-08-19', open: 230, high: 236, low: 229, close: 235, volume: 12 }] } }],
    }));
    expect(await screen.findByRole('button', { name: 'View AAPL 1Y Price & value data' })).toBeTruthy();
    await act(async () => initial.resolve(auction));
    expect(screen.queryByRole('button', { name: 'View AAPL 3M Price & value data' })).toBeNull();
  });

  it('requests a live 15-minute interval for the price chart', async () => {
    const deps = dependencies();
    render(<LensScreen {...deps.props} />);
    await waitFor(() => expect(deps.client.auction).toHaveBeenCalledWith({ ticker: 'AAPL', period: '3mo', interval: '1d' }, expect.anything()));
    fireEvent.press(screen.getByRole('tab', { name: 'Show 15 minute interval' }));
    await waitFor(() => expect(deps.client.auction).toHaveBeenCalledWith({ ticker: 'AAPL', period: '5d', interval: '15m' }, expect.anything()));
  });

  it('keeps cached evidence visible offline and disables retry until connectivity returns', async () => {
    const deps = dependencies({
      cached: cachedRecord(overview(), 20_000),
      chartCached: cachedRecord({ ...auction, datasets: [{ ...auction.datasets[0], period: '3mo' }] }, 20_000),
      reachability: 'offline',
      now: 100_000,
    });
    render(<LensScreen {...deps.props} />);

    expect(await screen.findByText('Offline · saved overview')).toBeTruthy();
    expect(screen.getByText('Apple Inc.')).toBeTruthy();
    expect(screen.getByText(/Fixture provider · Updated/)).toBeTruthy();
    expect(await screen.findByRole('button', { name: 'View AAPL 3M Price & value data' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Retry overview' }).props.accessibilityState.disabled).toBe(true);
    expect(deps.client.watchlistAlerts).not.toHaveBeenCalled();
    expect(deps.client.auction).not.toHaveBeenCalled();
  });

  it('keeps a stale chart visible when refresh fails and recovers on Retry', async () => {
    const cachedChart = { ...auction, datasets: [{ ...auction.datasets[0], period: '3mo' }] };
    const deps = dependencies({ chartCached: cachedRecord(cachedChart, 20_000), now: 400_000 });
    deps.client.auction
      .mockRejectedValueOnce(new Error('Price provider unavailable'))
      .mockResolvedValueOnce(cachedChart);
    render(<LensScreen {...deps.props} />);

    expect(await screen.findByText('Price provider unavailable')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'View AAPL 3M Price & value data' })).toBeTruthy();
    fireEvent.press(screen.getByRole('button', { name: 'Retry price chart' }));
    await waitFor(() => expect(deps.client.auction).toHaveBeenCalledTimes(2));
    expect(await screen.findByRole('button', { name: 'View AAPL 3M Price & value data' })).toBeTruthy();
    await waitFor(() => expect(screen.queryByText('Price provider unavailable')).toBeNull());
  });

  it('keeps live chart data visible when offline persistence fails', async () => {
    const deps = dependencies();
    deps.cache.write.mockImplementation(async (descriptor?: { route?: string }) => {
      if (descriptor?.route === API_ENDPOINTS.auction) throw new Error('Storage unavailable');
    });
    render(<LensScreen {...deps.props} />);

    expect(await screen.findByRole('button', { name: 'View AAPL 3M Price & value data' })).toBeTruthy();
    expect(await screen.findByText('Chart loaded, but it could not be saved for offline use.')).toBeTruthy();
  });

  it('distinguishes unreadable chart storage from a first-time offline cache miss', async () => {
    const deps = dependencies({ reachability: 'offline' });
    deps.cache.read.mockImplementation(async (descriptor: { route?: string }) => {
      if (descriptor.route === API_ENDPOINTS.auction) throw new Error('Storage unavailable');
      return null;
    });
    render(<LensScreen {...deps.props} />);

    expect(await screen.findByText('Saved chart storage could not be read. Reconnect to load this chart.')).toBeTruthy();
    expect(deps.client.auction).not.toHaveBeenCalled();
  });

  it('cancels an active overview and switches to offline truth when connectivity drops', async () => {
    const live = deferred<WatchlistAlertsResponse>();
    const deps = dependencies({ live: live.promise });
    let signal: AbortSignal | undefined;
    deps.client.watchlistAlerts.mockImplementation((_request?: unknown, options?: { signal: AbortSignal }) => {
      signal = options?.signal;
      return live.promise;
    });
    const view = render(<LensScreen {...deps.props} />);
    expect(await screen.findByText('Loading security overview')).toBeTruthy();

    view.rerender(<LensScreen {...deps.props} reachability="offline" />);
    expect(await screen.findByText('No saved overview offline')).toBeTruthy();
    expect(signal?.aborted).toBe(true);
    await act(async () => live.resolve(overview()));
    expect(screen.queryByText('Apple Inc.')).toBeNull();
  });

  it('shows stale cached evidence while refreshing and replaces it with the live snapshot', async () => {
    const live = deferred<WatchlistAlertsResponse>();
    const stale = overview({
      rows: [{ ...overview().rows[0], price: 220, setup: 'Cached setup.' }],
    });
    const deps = dependencies({ cached: cachedRecord(stale, 20_000), live: live.promise, now: 100_000 });
    render(<LensScreen {...deps.props} />);

    expect(await screen.findByText('Saved overview · refreshing')).toBeTruthy();
    expect(screen.getByText('$220.00')).toBeTruthy();
    await act(async () => live.resolve(overview()));
    expect(await screen.findByText('$231.42')).toBeTruthy();
    expect(screen.queryByText('$220.00')).toBeNull();
    expect(deps.cache.write).toHaveBeenCalled();
  });

  it('shows a retryable overview error without exposing stale security data', async () => {
    const deps = dependencies({ liveError: new Error('Overview provider unavailable') });
    render(<LensScreen {...deps.props} />);

    expect(await screen.findByText('Overview provider unavailable')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Retry overview' })).toBeTruthy();
    expect(screen.queryByText('Apple Inc.')).toBeNull();
  });

  it('rejects a cross-ticker overview instead of exposing another security', async () => {
    const mismatched = overview({
      rows: [{ ...overview().rows[0], ticker: 'MSFT', name: 'Microsoft' }],
    });
    const deps = dependencies({ live: Promise.resolve(mismatched) });
    render(<LensScreen {...deps.props} />);

    expect(await screen.findByText('Overview response did not match AAPL.')).toBeTruthy();
    expect(screen.queryByText('Microsoft')).toBeNull();
  });

  it('labels missing fundamentals honestly instead of inventing business evidence', async () => {
    const missing = overview();
    const response = overview({
      rows: [{
        ...missing.rows[0],
        fundamentals: {
          ...missing.rows[0].fundamentals,
          businessSummary: null,
          marketCap: null,
          trailingPe: null,
          forwardPe: null,
          revenueGrowth: null,
          profitMargins: null,
          fiftyTwoWeekHigh: null,
          fiftyTwoWeekLow: null,
          targetMeanPrice: null,
          analystCount: null,
        },
      }],
    });
    const deps = dependencies({ live: Promise.resolve(response) });
    render(<LensScreen {...deps.props} />);

    expect(await screen.findByText('Business summary unavailable.')).toBeTruthy();
    expect(screen.getAllByText('Unavailable')).toHaveLength(7);
    expect(screen.getByText('Analyst count unavailable')).toBeTruthy();
  });

  it('opens Glance with Torque and a separate 5d Auction, then Diagnose adds Moneyline', async () => {
    const deps = dependencies();
    render(<LensScreen {...deps.props} />);
    await screen.findByText('Apple Inc.');

    fireEvent.press(screen.getByRole('button', { name: 'Open Glance' }));
    expect(deps.client.torque).toHaveBeenCalledWith({ ticker: 'AAPL', period: '2y', interval: '1d' }, expect.anything());
    expect(deps.client.auction).toHaveBeenCalledWith({ ticker: 'AAPL', period: '5d', interval: '1d' }, expect.anything());
    expect(deps.client.moneyline).not.toHaveBeenCalled();
    expect(await screen.findByText(/Opened depth: Glance/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'View AAPL Torque data' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'View AAPL 3M Price & value data' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'View AAPL 5d Auction data' })).toBeTruthy();

    fireEvent.press(screen.getByRole('button', { name: 'Select Diagnose' }));
    fireEvent.press(screen.getByRole('button', { name: 'Open Diagnose' }));
    await waitFor(() => expect(deps.client.moneyline).toHaveBeenCalledWith({ ticker: 'AAPL' }, expect.anything()));
    expect(deps.client.torque).toHaveBeenCalledTimes(1);
    expect(deps.client.auction).toHaveBeenCalledTimes(2);
    expect(await screen.findByRole('button', { name: 'View AAPL Moneyline data' })).toBeTruthy();
  });

  it('deep dive navigates with normalized params without auto-running specialist charts', async () => {
    const deps = dependencies();
    render(<LensScreen {...deps.props} />);
    await screen.findByText('Apple Inc.');
    fireEvent.press(screen.getByRole('button', { name: 'Select Deep Dive' }));
    fireEvent.press(screen.getByRole('button', { name: 'Start Deep Dive' }));

    expect(deps.router.push).toHaveBeenCalledWith({
      pathname: '/research',
      params: { symbol: 'AAPL', period: '1y', depth: 'deep-dive' },
    });
    expect(deps.client.torque).not.toHaveBeenCalled();
    expect(deps.client.auction).toHaveBeenCalledWith({ ticker: 'AAPL', period: '3mo', interval: '1d' }, expect.anything());
    expect(deps.client.moneyline).not.toHaveBeenCalled();
    expect(deps.client).not.toHaveProperty('agentChat');
  });

  it('keeps panels independent, uses honest provenance, and exposes manual Retry', async () => {
    const deps = dependencies();
    deps.client.auction
      .mockResolvedValueOnce(auction)
      .mockRejectedValueOnce(new Error('Auction provider unavailable'))
      .mockResolvedValueOnce(auction);
    deps.client.moneyline.mockResolvedValue({ ...moneyline, rows: [] });
    render(<LensScreen {...deps.props} />);
    await screen.findByText('Apple Inc.');
    fireEvent.press(screen.getByRole('button', { name: 'Open Glance' }));

    expect(await screen.findByRole('button', { name: 'View AAPL Torque data' })).toBeTruthy();
    expect(screen.getByText(/Fundamental data unavailable/)).toBeTruthy();
    expect(screen.getAllByText('Source not reported').length).toBeGreaterThan(0);
    expect(await screen.findByText('Auction provider unavailable')).toBeTruthy();
    fireEvent.press(screen.getByRole('button', { name: 'Retry AAPL Auction' }));
    expect(await screen.findByRole('button', { name: 'View AAPL 5d Auction data' })).toBeTruthy();

    fireEvent.press(screen.getByRole('button', { name: 'Select Diagnose' }));
    fireEvent.press(screen.getByRole('button', { name: 'Open Diagnose' }));
    expect(await screen.findByText('Options positioning is unavailable.')).toBeTruthy();
  });

  it('marks every cross-ticker response unavailable instead of rendering another symbol', async () => {
    const deps = dependencies();
    deps.client.torque.mockResolvedValue({ ...torque, ticker: 'MSFT' });
    deps.client.auction.mockResolvedValue({
      ...auction,
      datasets: [{ ...auction.datasets[0], ticker: 'MSFT' }],
    });
    deps.client.moneyline.mockResolvedValue({ ...moneyline, ticker: 'MSFT' });
    render(<LensScreen {...deps.props} />);
    await screen.findByText('Apple Inc.');
    fireEvent.press(screen.getByRole('button', { name: 'Select Diagnose' }));
    fireEvent.press(screen.getByRole('button', { name: 'Open Diagnose' }));

    expect(await screen.findByText('Torque response did not match AAPL.')).toBeTruthy();
    expect(await screen.findByText('Auction response did not match AAPL.')).toBeTruthy();
    expect(await screen.findByText('Moneyline response did not match AAPL.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /View MSFT/ })).toBeNull();
  });

  it('ignores a replaced generation and aborts active work on unmount', async () => {
    const deps = dependencies();
    const first = deferred<typeof torque>();
    const second = deferred<typeof torque>();
    const third = deferred<typeof torque>();
    let firstSignal: AbortSignal | undefined;
    let thirdSignal: AbortSignal | undefined;
    deps.client.torque
      .mockImplementationOnce((_request?: unknown, options?: { signal: AbortSignal }) => {
        firstSignal = options?.signal;
        return first.promise;
      })
      .mockImplementationOnce(() => second.promise)
      .mockImplementationOnce((_request?: unknown, options?: { signal: AbortSignal }) => {
        thirdSignal = options?.signal;
        return third.promise;
      });
    const view = render(<LensScreen {...deps.props} />);
    await screen.findByText('Apple Inc.');
    fireEvent.press(screen.getByRole('button', { name: 'Open Glance' }));
    fireEvent.press(screen.getByRole('button', { name: 'Open Glance' }));
    await act(async () => second.resolve({ ...torque, series: { ...torque.series, price: { close: [{ date: '2026-08-18', value: 222 }] } } }));
    fireEvent.press(await screen.findByRole('button', { name: 'View AAPL Torque data' }));
    expect(screen.getByText('222')).toBeTruthy();
    await act(async () => first.resolve(torque));
    expect(screen.queryByText('220')).toBeNull();

    fireEvent.press(screen.getByRole('button', { name: 'Open Glance' }));
    view.unmount();
    expect(firstSignal?.aborted).toBe(true);
    expect(thirdSignal?.aborted).toBe(true);
  });

  it('rejects invalid route symbols without requesting data', () => {
    const deps = dependencies();
    render(<LensScreen {...deps.props} symbol="AAPL/../../research" />);
    expect(screen.getAllByText(/invalid ticker symbol/i).length).toBeGreaterThan(0);
    expect(deps.client.torque).not.toHaveBeenCalled();
    expect(deps.client.auction).not.toHaveBeenCalled();
  });

  it.each([
    [320, 1],
    [375, 1],
    [430, 1.6],
  ])('keeps one-column reflow and 44-point controls at %ipx / %ix font', async (width, fontScale) => {
    const deps = dependencies();
    render(<LensScreen {...deps.props} fontScale={fontScale} width={width} />);
    await screen.findByText('Apple Inc.');
    const action = screen.getByRole('button', { name: 'Open Glance' });
    expect(StyleSheet.flatten(action.props.style).minHeight).toBeGreaterThanOrEqual(44);
    expect(StyleSheet.flatten(screen.getByTestId('lens-content').props.contentContainerStyle)).not.toHaveProperty('height');
    expect(screen.getAllByText(/Glance opens Torque and 5d Auction/).every((text) => text.props.numberOfLines === undefined)).toBe(true);
  });
});
