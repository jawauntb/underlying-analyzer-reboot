import { fireEvent, render, screen, waitFor } from '@testing-library/react-native';
import { StrictMode } from 'react';
import { StyleSheet } from 'react-native';

import { MOBILE_AGENT_TOOLS } from '@/src/api/agentTools';
import LibraryScreen from '@/src/features/library/LibraryScreen';
import type { LibraryRecord } from '@/src/features/library/library-store';

jest.mock('@expo/vector-icons/Ionicons', () => {
  const React = jest.requireActual('react');
  const { Text } = jest.requireActual('react-native');
  return function MockIonicon({ name }: { name: string }) {
    return React.createElement(Text, null, name);
  };
});

jest.mock('react-native-safe-area-context', () => {
  const React = jest.requireActual('react');
  const { View } = jest.requireActual('react-native');
  return { SafeAreaView: ({ children, ...props }: { children?: React.ReactNode }) => React.createElement(View, props, children) };
});

function record(overrides: Partial<LibraryRecord> = {}): LibraryRecord {
  return {
    schemaVersion: 1,
    id: 'run-1',
    status: 'completed',
    symbol: 'AAPL',
    period: '1y',
    summary: 'Saved AAPL thesis with source disagreement.',
    model: 'claude-sonnet',
    tools: [...MOBILE_AGENT_TOOLS],
    toolTrace: [{ name: 'analyze_ticker', status: 'completed', durationMs: 24, error: null }],
    artifacts: [],
    source: { kind: 'research-agent', transport: 'stream' },
    generatedAt: 100,
    cachedAt: 200,
    accessedAt: 300,
    ...overrides,
  };
}

function dependencies(records = [record()]) {
  const store = {
    list: jest.fn(async () => ({ records, corruptedCount: 0 })),
    delete: jest.fn(async () => undefined),
    clear: jest.fn(async () => undefined),
  };
  const router = { push: jest.fn() };
  return { store, router, props: { store: store as never, router: router as never, focused: true, width: 375 } };
}

describe('LibraryScreen', () => {
  it('hydrates completed research offline and reopens the saved AAPL result', async () => {
    const deps = dependencies();
    render(<LibraryScreen {...deps.props} />);

    expect(await screen.findByText('Saved AAPL thesis with source disagreement.')).toBeTruthy();
    expect(screen.getAllByText('On this device').length).toBeGreaterThan(0);
    const open = screen.getByRole('button', { name: 'Open saved AAPL research' });
    fireEvent.press(open);
    expect(deps.router.push).toHaveBeenCalledWith({
      pathname: '/research',
      params: { symbol: 'AAPL', period: '1y', recordId: 'run-1' },
    });
    expect(StyleSheet.flatten(open.props.style).minHeight).toBeGreaterThanOrEqual(44);
  });

  it('deletes one record and does not return it to the rendered archive', async () => {
    const deps = dependencies([record(), record({ id: 'run-2', symbol: 'MSFT', summary: 'Saved MSFT thesis' })]);
    render(<LibraryScreen {...deps.props} />);
    await screen.findByText('Saved AAPL thesis with source disagreement.');
    fireEvent.press(screen.getByRole('button', { name: 'Delete saved AAPL research' }));
    await waitFor(() => expect(deps.store.delete).toHaveBeenCalledWith('run-1'));
    expect(screen.queryByText('Saved AAPL thesis with source disagreement.')).toBeNull();
    expect(screen.getByText('Saved MSFT thesis')).toBeTruthy();
  });

  it('requires an explicit confirmation before Clear All', async () => {
    const deps = dependencies();
    render(<LibraryScreen {...deps.props} />);
    await screen.findByText('Saved AAPL thesis with source disagreement.');

    fireEvent.press(screen.getByRole('button', { name: 'Clear all saved research' }));
    expect(screen.getByRole('alert')).toHaveTextContent(/cannot be undone/i);
    expect(deps.store.clear).not.toHaveBeenCalled();
    fireEvent.press(screen.getByRole('button', { name: 'Cancel clearing Library' }));
    expect(screen.queryByRole('alert')).toBeNull();
    fireEvent.press(screen.getByRole('button', { name: 'Clear all saved research' }));
    fireEvent.press(screen.getByRole('button', { name: 'Confirm clear all saved research' }));
    await waitFor(() => expect(deps.store.clear).toHaveBeenCalledTimes(1));
    expect(await screen.findByText('Nothing saved yet')).toBeTruthy();
  });

  it('recovers from corruption and keeps a reflowing 44-point empty action', async () => {
    const deps = dependencies([]);
    deps.store.list.mockResolvedValue({ records: [], corruptedCount: 2 });
    render(<LibraryScreen {...deps.props} width={320} />);

    expect(await screen.findByText(/Removed 2 unreadable saved records/)).toBeTruthy();
    expect(screen.getByText('Nothing saved yet')).toBeTruthy();
    const explore = screen.getByRole('button', { name: 'Open AAPL Ticker Lens' });
    expect(StyleSheet.flatten(explore.props.style).minHeight).toBeGreaterThanOrEqual(44);
    expect(StyleSheet.flatten(screen.getByTestId('library-content').props.style)).not.toHaveProperty('height');
  });

  it('shows hydration errors without making network assumptions', async () => {
    const deps = dependencies([]);
    deps.store.list.mockRejectedValue(new Error('Local storage unavailable'));
    render(<LibraryScreen {...deps.props} />);
    expect(await screen.findByText('Local storage unavailable')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Retry Library' })).toBeTruthy();
  });

  it('restores mounted lifecycle state across StrictMode effect replay', async () => {
    const deps = dependencies();
    render(<StrictMode><LibraryScreen {...deps.props} /></StrictMode>);
    expect(await screen.findByText('Saved AAPL thesis with source disagreement.')).toBeTruthy();
  });
});
