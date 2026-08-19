import { act, fireEvent, render, screen, waitFor } from '@testing-library/react-native';
import { StyleSheet } from 'react-native';

import SearchScreen from '@/src/features/search/SearchScreen';
import {
  RECENT_SEARCHES_STORAGE_KEY,
  RecentSearchStore,
} from '@/src/features/search/recent-searches';

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
  return {
    SafeAreaView: ({ children, ...props }: { children?: React.ReactNode }) =>
      React.createElement(View, props, children),
  };
});

const apple = {
  symbol: 'AAPL',
  name: 'Apple Inc.',
  exchange: 'NasdaqGS',
  assetType: 'equity' as const,
};

const alphabet = {
  symbol: 'GOOGL',
  name: 'Alphabet Inc.',
  exchange: 'NasdaqGS',
  assetType: 'equity' as const,
};

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((next) => { resolve = next; });
  return { promise, resolve };
};

function dependencies() {
  const client = {
    searchSecurities: jest.fn(),
    watchlistAlerts: jest.fn(),
    auction: jest.fn(),
    torque: jest.fn(),
    moneyline: jest.fn(),
    agentChat: jest.fn(),
    agentStream: jest.fn(),
  };
  const recentStore = {
    hydrate: jest.fn(async (): Promise<any[]> => []),
    record: jest.fn(async (_result: unknown): Promise<void> => undefined),
  };
  const listsState = {
    hydrated: true,
    lists: [],
  };
  const router = { push: jest.fn() };
  return {
    client,
    recentStore,
    listsState,
    router,
    props: {
      client: client as never,
      recentStore: recentStore as never,
      listsState: listsState as never,
      router: router as never,
    },
  };
}

describe('SearchScreen', () => {
  beforeEach(() => {
    jest.useFakeTimers();
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  it('waits for a meaningful query, debounces typing, aborts stale work, and never starts heavy endpoints', async () => {
    const deps = dependencies();
    const first = deferred<any>();
    const second = deferred<any>();
    deps.client.searchSecurities
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise);

    const view = render(<SearchScreen {...deps.props} />);
    expect(deps.client.searchSecurities).not.toHaveBeenCalled();
    await act(async () => undefined);

    fireEvent.changeText(screen.getByLabelText('Search companies and tickers'), 'ap');
    act(() => jest.advanceTimersByTime(249));
    expect(deps.client.searchSecurities).not.toHaveBeenCalled();
    await act(async () => jest.advanceTimersByTime(1));
    expect(deps.client.searchSecurities).toHaveBeenCalledTimes(1);
    const firstSignal = deps.client.searchSecurities.mock.calls[0][1].signal as AbortSignal;

    fireEvent.changeText(screen.getByLabelText('Search companies and tickers'), 'app');
    await act(async () => jest.advanceTimersByTime(250));
    expect(deps.client.searchSecurities).toHaveBeenCalledTimes(2);
    expect(firstSignal.aborted).toBe(true);

    await act(async () => second.resolve({ query: 'app', results: [apple], provider: 'Yahoo Finance' }));
    expect(await screen.findByRole('button', { name: 'Open AAPL Apple Inc. Lens' })).toBeTruthy();
    expect(screen.getByText('Results from Yahoo Finance')).toBeTruthy();

    await act(async () => first.resolve({ query: 'ap', results: [alphabet], provider: 'Stale provider' }));
    expect(screen.queryByRole('button', { name: /GOOGL/ })).toBeNull();

    view.unmount();
    expect(deps.client.watchlistAlerts).not.toHaveBeenCalled();
    expect(deps.client.auction).not.toHaveBeenCalled();
    expect(deps.client.torque).not.toHaveBeenCalled();
    expect(deps.client.moneyline).not.toHaveBeenCalled();
    expect(deps.client.agentChat).not.toHaveBeenCalled();
    expect(deps.client.agentStream).not.toHaveBeenCalled();
  });

  it('supports an explicit one-character ticker search without auto-submitting it', async () => {
    const deps = dependencies();
    deps.client.searchSecurities.mockResolvedValue({ query: 'F', results: [], provider: 'Yahoo Finance' });
    render(<SearchScreen {...deps.props} />);
    await act(async () => undefined);

    fireEvent.changeText(screen.getByLabelText('Search companies and tickers'), ' F ');
    await act(async () => jest.advanceTimersByTime(500));
    expect(deps.client.searchSecurities).not.toHaveBeenCalled();

    fireEvent(screen.getByLabelText('Search companies and tickers'), 'submitEditing');
    await waitFor(() => expect(deps.client.searchSecurities).toHaveBeenCalledWith(
      { query: 'F', limit: 10 },
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    ));
    expect(await screen.findByText('No matches for “F”')).toBeTruthy();
  });

  it('routes result and newest-list quick access with object params, then persists selections in the background', async () => {
    const deps = dependencies();
    deps.listsState.lists = [
      { id: 'old', name: 'Old', symbols: ['AAPL'], source: { kind: 'manual' }, createdAt: 1, updatedAt: 2 },
      { id: 'new', name: 'New', symbols: ['MSFT', 'MSFT', '^GSPC'], source: { kind: 'manual' }, createdAt: 2, updatedAt: 3 },
    ] as never;
    deps.client.searchSecurities.mockResolvedValue({ query: 'apple', results: [apple], provider: 'Yahoo Finance' });
    render(<SearchScreen {...deps.props} />);
    await act(async () => undefined);

    const msft = await screen.findByRole('button', { name: 'Open MSFT Lens from New' });
    expect(screen.getAllByText('MSFT')).toHaveLength(1);
    expect(StyleSheet.flatten(msft.props.style).minHeight).toBeGreaterThanOrEqual(44);
    fireEvent.press(msft);
    expect(deps.router.push).toHaveBeenLastCalledWith({ pathname: '/ticker/[symbol]', params: { symbol: 'MSFT' } });

    fireEvent.changeText(screen.getByLabelText('Search companies and tickers'), 'apple');
    await act(async () => jest.advanceTimersByTime(250));
    const result = await screen.findByRole('button', { name: 'Open AAPL Apple Inc. Lens' });
    fireEvent.press(result);
    expect(deps.router.push).toHaveBeenLastCalledWith({ pathname: '/ticker/[symbol]', params: { symbol: 'AAPL' } });
    expect(deps.recentStore.record).toHaveBeenCalledWith(apple);
    expect(StyleSheet.flatten(result.props.style).minHeight).toBeGreaterThanOrEqual(44);
  });

  it('shows recent selections before a query and uses defaults when no saved list exists', async () => {
    const deps = dependencies();
    deps.recentStore.hydrate.mockResolvedValue([{ ...apple, selectedAt: 7 }]);
    render(<SearchScreen {...deps.props} />);

    expect(await screen.findByRole('button', { name: 'Open recent AAPL Apple Inc. Lens' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Open AAPL Lens from Market starters' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Open MSFT Lens from Market starters' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Open NVDA Lens from Market starters' })).toBeTruthy();
  });

  it('shows an error with retry and recovers through the same lightweight search method', async () => {
    const deps = dependencies();
    deps.client.searchSecurities
      .mockRejectedValueOnce(new Error('Search service unavailable'))
      .mockResolvedValueOnce({ query: 'apple', results: [apple], provider: 'Yahoo Finance' });
    render(<SearchScreen {...deps.props} />);
    await act(async () => undefined);

    fireEvent.changeText(screen.getByLabelText('Search companies and tickers'), 'apple');
    await act(async () => jest.advanceTimersByTime(250));
    expect(await screen.findByRole('alert')).toBeTruthy();
    expect(screen.getByText('Search service unavailable')).toBeTruthy();
    fireEvent.press(screen.getByRole('button', { name: 'Retry search' }));
    expect(await screen.findByRole('button', { name: 'Open AAPL Apple Inc. Lens' })).toBeTruthy();
    expect(deps.client.searchSecurities).toHaveBeenCalledTimes(2);
  });
});

describe('RecentSearchStore', () => {
  it('deduplicates newest selections, persists a versioned envelope, and caps records at six', async () => {
    let timestamp = 10;
    const storage = {
      getItem: jest.fn(async (_key: string): Promise<string | null> => null),
      setItem: jest.fn(async (_key: string, _value: string): Promise<void> => undefined),
      removeItem: jest.fn(async (_key: string): Promise<void> => undefined),
    };
    const store = new RecentSearchStore(storage, { now: () => timestamp++ });
    await store.hydrate();
    for (const symbol of ['AAPL', 'MSFT', 'NVDA', 'GOOGL', 'AMZN', 'META', 'AAPL']) {
      await store.record({ ...apple, symbol, name: `${symbol} name` });
    }

    expect(store.snapshot().map((record) => record.symbol)).toEqual(['AAPL', 'META', 'AMZN', 'GOOGL', 'NVDA', 'MSFT']);
    expect(JSON.parse(storage.setItem.mock.calls.at(-1)![1])).toEqual({
      schemaVersion: 1,
      records: store.snapshot(),
    });
  });

  it('repairs partially corrupt records and removes an unreadable envelope', async () => {
    const valid = { ...apple, selectedAt: 42 };
    const storage = {
      getItem: jest.fn<Promise<string | null>, [string]>()
        .mockResolvedValueOnce(JSON.stringify({ schemaVersion: 1, records: [valid, { symbol: '../BAD' }] }))
        .mockResolvedValueOnce('{broken'),
      setItem: jest.fn(async (_key: string, _value: string): Promise<void> => undefined),
      removeItem: jest.fn(async (_key: string): Promise<void> => undefined),
    };
    const store = new RecentSearchStore(storage);

    await expect(store.hydrate()).resolves.toEqual([valid]);
    expect(storage.setItem).toHaveBeenLastCalledWith(
      RECENT_SEARCHES_STORAGE_KEY,
      JSON.stringify({ schemaVersion: 1, records: [valid] }),
    );
    await expect(store.hydrate()).resolves.toEqual([]);
    expect(storage.removeItem).toHaveBeenCalledWith(RECENT_SEARCHES_STORAGE_KEY);
  });
});
