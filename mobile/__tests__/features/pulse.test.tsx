import { act, fireEvent, render, screen, waitFor } from '@testing-library/react-native';
import React from 'react';
import { StyleSheet } from 'react-native';

import type { WatchlistAlertsResponse } from '@/src/api/contracts';
import PulseScreen from '@/src/features/pulse/PulseScreen';
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

const response = (overrides: Partial<WatchlistAlertsResponse> = {}): WatchlistAlertsResponse => ({
  status: 'fresh',
  rows: [{ ticker: 'AAPL', rank: 1, lane: 'Priority', name: 'Apple', sector: null, industry: null, price: 220, changePercent: 1.25, annualVolatility: null, scannerScore: 91, score: 88, setup: 'Breakout', provider: 'Yahoo', providerNote: null, ridge: {}, flow: {}, auction: {}, raw: {} }],
  alerts: [],
  digest: { generatedAt: '2026-08-19T12:00:00Z', headline: 'Ready', summary: 'One setup', severityCounts: {}, categoryCounts: {}, laneCounts: {}, priorityTickers: ['AAPL'], riskTickers: [], flowShiftTickers: [], nextSteps: [] },
  provider: 'Yahoo',
  providerNote: 'Primary quote feed',
  errors: [],
  meta: {},
  watchlist: null,
  tickers: ['AAPL'],
  ...overrides,
});

const record = (data: WatchlistAlertsResponse, fetchedAt: number): CacheRecord<WatchlistAlertsResponse> => ({
  schemaVersion: CACHE_SCHEMA_VERSION,
  data,
  fetchedAt,
  accessedAt: fetchedAt,
});

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
};

function dependencies(options: {
  cached?: CacheRecord<WatchlistAlertsResponse> | null;
  live?: Promise<WatchlistAlertsResponse>;
  reachability?: 'online' | 'offline' | 'unknown';
  now?: number;
} = {}) {
  const cache = { read: jest.fn(async () => options.cached ?? null), write: jest.fn(async () => undefined) };
  const client = { baseUrl: 'https://api.test', watchlistAlerts: jest.fn(() => options.live ?? Promise.resolve(response())) };
  const router = { push: jest.fn() };
  return {
    cache,
    client,
    router,
    props: {
      cache: cache as never,
      client: client as never,
      router: router as never,
      reachability: options.reachability ?? 'online',
      listsState: { hydrated: true, lists: [] },
      focused: true,
      now: () => options.now ?? 100_000,
    },
  };
}

describe('PulseScreen', () => {
  it('waits for list hydration and snapshots the newest list on the next focus', async () => {
    const deps = dependencies();
    const view = render(<PulseScreen {...deps.props} listsState={{ hydrated: false, lists: [] }} />);
    expect(screen.getByText(/loading your saved lists/i)).toBeTruthy();
    expect(deps.client.watchlistAlerts).not.toHaveBeenCalled();

    view.rerender(<PulseScreen {...deps.props} listsState={{ hydrated: true, lists: [] }} />);
    await waitFor(() => expect(deps.client.watchlistAlerts).toHaveBeenCalledWith({ tickers: ['AAPL', 'MSFT', 'NVDA'] }, expect.anything()));
    deps.client.watchlistAlerts.mockClear();

    const lists = [
      { id: 'older', name: 'Older', symbols: ['MSFT'], source: { kind: 'manual' as const }, createdAt: 1, updatedAt: 1 },
      { id: 'newest', name: 'Newest', symbols: ['NVDA', 'AAPL'], source: { kind: 'manual' as const }, createdAt: 2, updatedAt: 2 },
    ];
    view.rerender(<PulseScreen {...deps.props} focused={false} listsState={{ hydrated: true, lists }} />);
    view.rerender(<PulseScreen {...deps.props} focused listsState={{ hydrated: true, lists }} />);
    await waitFor(() => expect(deps.client.watchlistAlerts).toHaveBeenCalledWith({ tickers: ['NVDA', 'AAPL'] }, expect.anything()));
  });

  it('renders cached content before stale live completion and makes one alerts bootstrap only', async () => {
    const live = deferred<WatchlistAlertsResponse>();
    const cached = response({ rows: [{ ...response().rows[0], ticker: 'MSFT', name: 'Microsoft' }] });
    const deps = dependencies({ cached: record(cached, 0), live: live.promise });
    render(<PulseScreen {...deps.props} />);

    expect(await screen.findByText('MSFT')).toBeTruthy();
    expect(screen.getByText(/refreshing/i)).toBeTruthy();
    expect(deps.client.watchlistAlerts).toHaveBeenCalledTimes(1);
    expect(deps.client.watchlistAlerts).toHaveBeenCalledWith({ tickers: ['AAPL', 'MSFT', 'NVDA'] }, expect.anything());

    await act(async () => live.resolve(response()));
    expect(await screen.findByText('AAPL')).toBeTruthy();
    expect(screen.queryByText('MSFT')).toBeNull();
    expect(deps.cache.write).toHaveBeenCalledTimes(1);
  });

  it('uses fresh cache without any request', async () => {
    const deps = dependencies({ cached: record(response(), 99_999) });
    render(<PulseScreen {...deps.props} />);
    expect(await screen.findByText('AAPL')).toBeTruthy();
    expect(deps.client.watchlistAlerts).not.toHaveBeenCalled();
  });

  it('keeps partial rows visible with a per-ticker notice', async () => {
    const deps = dependencies({ live: Promise.resolve(response({ status: 'partial', errors: [{ ticker: 'MSFT', error: 'Quote unavailable' }] })) });
    render(<PulseScreen {...deps.props} />);
    expect(await screen.findByText('AAPL')).toBeTruthy();
    expect(screen.getByText(/MSFT.*Quote unavailable/i)).toBeTruthy();
  });

  it('ignores a late result from an older explicit generation', async () => {
    const first = deferred<WatchlistAlertsResponse>();
    const second = deferred<WatchlistAlertsResponse>();
    const deps = dependencies();
    render(<PulseScreen {...deps.props} />);
    await screen.findByText('AAPL');
    deps.client.watchlistAlerts.mockImplementationOnce(() => first.promise).mockImplementationOnce(() => second.promise);
    fireEvent.press(screen.getByRole('button', { name: /refresh pulse/i }));
    fireEvent.press(screen.getByRole('button', { name: /refresh pulse/i }));
    await act(async () => second.resolve(response({ rows: [{ ...response().rows[0], ticker: 'NVDA' }] })));
    expect(await screen.findByText('NVDA')).toBeTruthy();
    await act(async () => first.resolve(response({ rows: [{ ...response().rows[0], ticker: 'MSFT' }] })));
    expect(screen.queryByText('MSFT')).toBeNull();
  });

  it('always labels offline cache stale and disables retry without auto-retrying on reconnect', async () => {
    const deps = dependencies({ cached: record(response(), 99_999), reachability: 'offline' });
    const view = render(<PulseScreen {...deps.props} />);
    expect(await screen.findByText(/offline.*stale/i)).toBeTruthy();
    expect(screen.getByRole('button', { name: /retry pulse/i }).props.accessibilityState).toMatchObject({ disabled: true });
    view.rerender(<PulseScreen {...deps.props} reachability="online" />);
    await act(async () => undefined);
    expect(deps.client.watchlistAlerts).not.toHaveBeenCalled();
  });

  it('relabels cached content offline without turning reconnection into an automatic retry', async () => {
    const deps = dependencies({ cached: record(response(), 99_999) });
    const view = render(<PulseScreen {...deps.props} />);
    expect(await screen.findByText('AAPL')).toBeTruthy();
    view.rerender(<PulseScreen {...deps.props} reachability="offline" />);
    expect(await screen.findByText(/offline.*stale/i)).toBeTruthy();
    view.rerender(<PulseScreen {...deps.props} reachability="online" />);
    await act(async () => undefined);
    expect(deps.client.watchlistAlerts).not.toHaveBeenCalled();
  });

  it('renders empty-offline, empty-online, loading, and total error actionably', async () => {
    const offline = dependencies({ reachability: 'offline' });
    const offlineView = render(<PulseScreen {...offline.props} />);
    expect(await screen.findByText(/no cached pulse/i)).toBeTruthy();
    offlineView.unmount();

    const loading = deferred<WatchlistAlertsResponse>();
    const online = dependencies({ live: loading.promise });
    const onlineView = render(<PulseScreen {...online.props} />);
    expect(await screen.findByText(/loading market pulse/i)).toBeTruthy();
    await act(async () => loading.resolve(response({ rows: [] })));
    expect(await screen.findByText(/no setups matched/i)).toBeTruthy();
    onlineView.unmount();

    const failed = dependencies({ live: Promise.reject(new Error('provider down')) });
    render(<PulseScreen {...failed.props} />);
    expect(await screen.findByText(/provider down/i)).toBeTruthy();
    expect(screen.getByRole('button', { name: /retry pulse/i })).toBeTruthy();
  });

  it('supports explicit refresh and object-param ticker navigation without heavy endpoints', async () => {
    const deps = dependencies();
    render(<PulseScreen {...deps.props} />);
    fireEvent.press(await screen.findByRole('button', { name: /open AAPL lens/i }));
    expect(deps.router.push).toHaveBeenCalledWith({ pathname: '/ticker/[symbol]', params: { symbol: 'AAPL' } });
    fireEvent.press(screen.getByRole('button', { name: /refresh pulse/i }));
    await waitFor(() => expect(deps.client.watchlistAlerts).toHaveBeenCalledTimes(2));
    expect(Object.keys(deps.client)).toEqual(expect.arrayContaining(['baseUrl', 'watchlistAlerts']));
    expect(Object.keys(deps.client)).not.toEqual(expect.arrayContaining(['cockpit', 'agentChat', 'auction']));
    const action = screen.getByRole('button', { name: /refresh pulse/i });
    expect(StyleSheet.flatten(action.props.style).minHeight).toBeGreaterThanOrEqual(44);
  });
});
