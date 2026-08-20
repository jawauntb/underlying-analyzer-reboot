import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react-native';
import React from 'react';
import { StyleSheet } from 'react-native';

import ListsScreen from '@/src/features/lists/ListsScreen';
import {
  SavedListsProvider,
  type SavedListsContextValue,
  useSavedLists,
  WatchlistStore,
} from '@/src/features/lists/watchlists';

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

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => { resolve = res; });
  return { promise, resolve };
};

let observedListsState: SavedListsContextValue | null = null;

function SavedListsStateProbe() {
  observedListsState = useSavedLists();
  return null;
}

function currentListsState(): SavedListsContextValue {
  if (!observedListsState) throw new Error('Saved lists state was not observed.');
  return observedListsState;
}

function dependencies() {
  const saved = [{ id: 'old', name: 'Existing', symbols: ['AAPL'], source: { kind: 'manual' as const }, createdAt: 1, updatedAt: 1 }];
  const lists = {
    hydrated: true,
    hydrationError: null as string | null,
    droppedCorruptListCount: 0,
    lists: saved,
    retryHydration: jest.fn(),
    saveManual: jest.fn(async (name: string, symbols: string[]) => ({ ...saved[0], id: 'manual', name, symbols })),
    saveTradingView: jest.fn(async (input: unknown) => ({ ...saved[0], id: 'imported', ...(input as object) })),
    renameList: jest.fn(async (_id: string, name: string) => ({ ...saved[0], name: name.trim() })),
    addSymbol: jest.fn(async (_id: string, symbol: string) => ({ ...saved[0], symbols: ['AAPL', symbol.trim().toUpperCase()] })),
    removeSymbol: jest.fn(async () => { throw new Error('A list needs at least one symbol. Delete the list instead.'); }),
    deleteList: jest.fn(async () => undefined),
  };
  const client = { resolveWatchlist: jest.fn() };
  const router = { push: jest.fn() };
  return { client, lists, router, props: { client: client as never, listsState: lists as never, router: router as never } };
}

describe('ListsScreen', () => {
  it('renames, adds symbols, and requires a second press before deleting a saved list', async () => {
    const deps = dependencies();
    render(<ListsScreen {...deps.props} />);

    expect(screen.queryByLabelText('Rename Existing')).toBeNull();
    fireEvent.press(screen.getByRole('button', { name: 'Edit Existing' }));

    fireEvent.changeText(screen.getByLabelText('Rename Existing'), ' Core names ');
    fireEvent.press(screen.getByRole('button', { name: 'Save name for Existing' }));
    await waitFor(() => expect(deps.lists.renameList).toHaveBeenCalledWith('old', ' Core names '));

    fireEvent.changeText(screen.getByLabelText('New symbol for Existing'), 'nvda');
    fireEvent.press(screen.getByRole('button', { name: 'Add NVDA to Existing' }));
    await waitFor(() => expect(deps.lists.addSymbol).toHaveBeenCalledWith('old', 'nvda'));

    // The store refuses to strand an empty list, and the refusal reaches the reader.
    fireEvent.press(screen.getByRole('button', { name: 'Remove AAPL from Existing' }));
    expect(await screen.findByText('A list needs at least one symbol. Delete the list instead.')).toBeTruthy();

    fireEvent.press(screen.getByRole('button', { name: 'Delete Existing' }));
    expect(deps.lists.deleteList).not.toHaveBeenCalled();
    fireEvent.press(screen.getByRole('button', { name: 'Confirm delete Existing' }));
    await waitFor(() => expect(deps.lists.deleteList).toHaveBeenCalledWith('old'));
  });


  it('saves a normalized manual list and preserves existing browsing', async () => {
    const deps = dependencies();
    render(<ListsScreen {...deps.props} />);
    expect(screen.getByText('Existing')).toBeTruthy();
    fireEvent.changeText(screen.getByLabelText('Manual list name'), ' Mega cap ');
    fireEvent.changeText(screen.getByLabelText('Manual symbols'), ' msft, aapl MSFT ');
    fireEvent.press(screen.getByRole('button', { name: 'Save manual list' }));
    await waitFor(() => expect(deps.lists.saveManual).toHaveBeenCalledWith('Mega cap', ['MSFT', 'AAPL']));
  });

  it.each([
    ['bad token', 'AAPL BAD/SYMBOL', /invalid symbol/i],
    ['too many', 'A B C D E F G H I J K', /10 symbols/i],
  ])('rejects %s without losing earlier lists', async (_label, symbols, message) => {
    const deps = dependencies();
    render(<ListsScreen {...deps.props} />);
    fireEvent.changeText(screen.getByLabelText('Manual list name'), 'Invalid');
    fireEvent.changeText(screen.getByLabelText('Manual symbols'), symbols);
    fireEvent.press(screen.getByRole('button', { name: 'Save manual list' }));
    expect(await screen.findByText(message)).toBeTruthy();
    expect(screen.getByText('Existing')).toBeTruthy();
    expect(deps.lists.saveManual).not.toHaveBeenCalled();
  });

  it.each([
    'http://www.tradingview.com/watchlists/123/',
    'https://user:pass@www.tradingview.com/watchlists/123/',
    'https://www.tradingview.com/watchlists/private/',
    'malformed',
  ])('validates %s before any preview request', async (url) => {
    const deps = dependencies();
    render(<ListsScreen {...deps.props} />);
    fireEvent.changeText(screen.getByLabelText('TradingView watchlist URL'), url);
    fireEvent.press(screen.getByRole('button', { name: 'Preview import' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/TradingView/i);
    expect(deps.client.resolveWatchlist).not.toHaveBeenCalled();
    expect(screen.getByText('Existing')).toBeTruthy();
  });

  it('uses the backend-truncated top-level preview and saves as a new canonical list', async () => {
    const deps = dependencies();
    deps.client.resolveWatchlist.mockResolvedValue({
      watchlist: { id: 123, name: 'Remote name', sourceUrl: 'https://www.tradingview.com/watchlists/123/', tickers: ['AAPL', 'MSFT', 'NVDA'] },
      tickers: ['AAPL', 'MSFT'],
      maxResults: 10,
    });
    render(<ListsScreen {...deps.props} />);
    fireEvent.changeText(screen.getByLabelText('TradingView watchlist URL'), 'https://www.tradingview.com/watchlists/123/');
    fireEvent.press(screen.getByRole('button', { name: 'Preview import' }));
    expect(await screen.findByText('AAPL, MSFT')).toBeTruthy();
    expect(screen.queryByText(/NVDA/)).toBeNull();
    expect(deps.client.resolveWatchlist).toHaveBeenCalledWith({ watchlistUrl: 'https://www.tradingview.com/watchlists/123/', maxResults: 10 }, expect.anything());
    fireEvent.changeText(screen.getByLabelText('Preview list name'), 'Edited name');
    fireEvent.press(screen.getByRole('button', { name: 'Save as new list' }));
    await waitFor(() => expect(deps.lists.saveTradingView).toHaveBeenCalledWith({ name: 'Edited name', symbols: ['AAPL', 'MSFT'], sourceUrl: 'https://www.tradingview.com/watchlists/123/', remoteId: '123' }));
    expect(screen.getByText('Existing')).toBeTruthy();
  });

  it('rejects a backend preview that switches the requested watchlist id', async () => {
    const deps = dependencies();
    deps.client.resolveWatchlist.mockResolvedValue({
      watchlist: { id: 456, name: 'Switched', sourceUrl: 'https://www.tradingview.com/watchlists/456/', tickers: ['AAPL'] },
      tickers: ['AAPL'],
      maxResults: 10,
    });
    render(<ListsScreen {...deps.props} />);
    fireEvent.changeText(screen.getByLabelText('TradingView watchlist URL'), 'https://www.tradingview.com/watchlists/123/');
    fireEvent.press(screen.getByRole('button', { name: 'Preview import' }));
    expect(await screen.findByText(/does not match/i)).toBeTruthy();
    expect(screen.queryByLabelText('TradingView import preview')).toBeNull();
    expect(screen.getByText('Existing')).toBeTruthy();
  });

  it('keeps earlier lists when the provider rejects an import', async () => {
    const deps = dependencies();
    deps.client.resolveWatchlist.mockRejectedValue(new Error('Private watchlist'));
    render(<ListsScreen {...deps.props} />);
    fireEvent.changeText(screen.getByLabelText('TradingView watchlist URL'), 'https://www.tradingview.com/watchlists/123/');
    fireEvent.press(screen.getByRole('button', { name: 'Preview import' }));
    expect(await screen.findByText('Private watchlist')).toBeTruthy();
    expect(screen.getByText('Existing')).toBeTruthy();
    expect(deps.lists.saveTradingView).not.toHaveBeenCalled();
  });

  it('aborts/replaces preview and ignores stale completion', async () => {
    const deps = dependencies();
    const first = deferred<any>();
    const second = deferred<any>();
    deps.client.resolveWatchlist.mockImplementationOnce(() => first.promise).mockImplementationOnce(() => second.promise);
    render(<ListsScreen {...deps.props} />);
    fireEvent.changeText(screen.getByLabelText('TradingView watchlist URL'), 'https://www.tradingview.com/watchlists/1/');
    fireEvent.press(screen.getByRole('button', { name: 'Preview import' }));
    fireEvent.changeText(screen.getByLabelText('TradingView watchlist URL'), 'https://www.tradingview.com/watchlists/2/');
    fireEvent.press(screen.getByRole('button', { name: 'Preview import' }));
    await act(async () => second.resolve({ watchlist: { id: 2, name: 'Second', sourceUrl: 'https://www.tradingview.com/watchlists/2/', tickers: ['MSFT'] }, tickers: ['MSFT'], maxResults: 10 }));
    expect(within(await screen.findByLabelText('TradingView import preview')).getByText('MSFT')).toBeTruthy();
    await act(async () => first.resolve({ watchlist: { id: 1, name: 'First', sourceUrl: 'https://www.tradingview.com/watchlists/1/', tickers: ['AAPL'] }, tickers: ['AAPL'], maxResults: 10 }));
    expect(within(screen.getByLabelText('TradingView import preview')).queryByText('AAPL')).toBeNull();
  });

  it('routes saved symbols through object params and exposes 44-point reflowing controls', () => {
    const deps = dependencies();
    render(<ListsScreen {...deps.props} />);
    const symbol = screen.getByRole('button', { name: 'Open AAPL Lens' });
    fireEvent.press(symbol);
    expect(deps.router.push).toHaveBeenCalledWith({ pathname: '/ticker/[symbol]', params: { symbol: 'AAPL' } });
    expect(StyleSheet.flatten(symbol.props.style).minHeight).toBeGreaterThanOrEqual(44);
    expect(StyleSheet.flatten(screen.getByLabelText('Saved list Existing').props.style)).not.toHaveProperty('height');
  });

  it('keeps writes disabled after hydration fails and exposes a retry action', () => {
    const deps = dependencies();
    deps.lists.hydrated = false;
    deps.lists.hydrationError = 'Saved lists could not be read from this device. Try again.';
    render(<ListsScreen {...deps.props} />);

    expect(screen.getByText('Saved lists unavailable')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Save manual list' })).toBeDisabled();
    fireEvent.press(screen.getByRole('button', { name: 'Retry saved lists' }));
    expect(deps.lists.retryHydration).toHaveBeenCalledTimes(1);
  });

  it('shows how many unreadable saved lists were removed while keeping survivors visible', () => {
    const deps = dependencies();
    deps.lists.droppedCorruptListCount = 2;
    render(<ListsScreen {...deps.props} />);

    expect(screen.getByText('Saved lists repaired')).toBeTruthy();
    expect(screen.getByText('2 unreadable saved lists were removed. Your other lists are still here.')).toBeTruthy();
    expect(screen.getByText('Existing')).toBeTruthy();
  });
});

describe('SavedListsProvider', () => {
  it('stays unready after a read failure, blocks writes, and retries hydration', async () => {
    let readAttempts = 0;
    const storage = {
      getItem: jest.fn(async () => {
        readAttempts += 1;
        if (readAttempts === 1) throw new Error('storage unavailable');
        return null;
      }),
      setItem: jest.fn(async () => undefined),
      removeItem: jest.fn(async () => undefined),
    };
    const store = new WatchlistStore(storage, { createId: () => 'ready', now: () => 1 });
    observedListsState = null;
    render(
      <SavedListsProvider store={store}>
        <SavedListsStateProbe />
      </SavedListsProvider>,
    );

    await waitFor(() => expect(currentListsState().hydrationError).toMatch(/could not be read/i));
    expect(currentListsState().hydrated).toBe(false);
    await expect(currentListsState().saveManual('Blocked', ['AAPL'])).rejects.toThrow(/not ready/i);
    expect(storage.setItem).not.toHaveBeenCalled();

    act(() => currentListsState().retryHydration());
    await waitFor(() => expect(currentListsState().hydrated).toBe(true));
    await act(async () => {
      await currentListsState().saveManual('Ready', ['AAPL']);
    });
    expect(storage.setItem).toHaveBeenCalledTimes(1);
  });
});
