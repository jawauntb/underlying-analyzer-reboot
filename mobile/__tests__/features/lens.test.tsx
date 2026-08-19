import { act, fireEvent, render, screen, waitFor } from '@testing-library/react-native';
import { StyleSheet } from 'react-native';

import { generateStaticParams } from '@/app/ticker/[symbol]';
import LensScreen from '@/src/features/lens/LensScreen';

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

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
};

function dependencies() {
  const client = {
    torque: jest.fn(async () => torque),
    auction: jest.fn(async () => auction),
    moneyline: jest.fn(async () => moneyline),
  };
  const router = { push: jest.fn() };
  const haptics = { selectionAsync: jest.fn(async () => undefined) };
  return { client, router, haptics, props: { client: client as never, router: router as never, haptics, symbol: ' aapl ', width: 375, fontScale: 1 } };
}

describe('LensScreen', () => {
  it('normalizes the ticker, retains static AAPL, and fetches nothing on mount or depth selection', () => {
    const deps = dependencies();
    render(<LensScreen {...deps.props} />);

    expect(screen.getByRole('header', { name: 'AAPL' })).toBeTruthy();
    expect(generateStaticParams()).toEqual(expect.arrayContaining([{ symbol: 'AAPL' }]));
    expect(screen.getByText(/Selected depth: Glance/)).toBeTruthy();
    expect(screen.getByText(/Opened depth: None/)).toBeTruthy();
    fireEvent.press(screen.getByRole('button', { name: 'Select Diagnose' }));
    expect(screen.getByText(/Selected depth: Diagnose/)).toBeTruthy();
    expect(screen.getByText(/Opened depth: None/)).toBeTruthy();
    expect(deps.client.torque).not.toHaveBeenCalled();
    expect(deps.client.auction).not.toHaveBeenCalled();
    expect(deps.client.moneyline).not.toHaveBeenCalled();
  });

  it('opens Glance with only Torque and 5d Auction, then Diagnose adds Moneyline', async () => {
    const deps = dependencies();
    render(<LensScreen {...deps.props} />);

    fireEvent.press(screen.getByRole('button', { name: 'Open Glance' }));
    expect(deps.client.torque).toHaveBeenCalledWith({ ticker: 'AAPL' }, expect.anything());
    expect(deps.client.auction).toHaveBeenCalledWith({ ticker: 'AAPL', period: '5d' }, expect.anything());
    expect(deps.client.moneyline).not.toHaveBeenCalled();
    expect(await screen.findByText(/Opened depth: Glance/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'View AAPL Torque data' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'View AAPL 5d Auction data' })).toBeTruthy();

    fireEvent.press(screen.getByRole('button', { name: 'Select Diagnose' }));
    fireEvent.press(screen.getByRole('button', { name: 'Open Diagnose' }));
    await waitFor(() => expect(deps.client.moneyline).toHaveBeenCalledWith({ ticker: 'AAPL' }, expect.anything()));
    expect(deps.client.torque).toHaveBeenCalledTimes(1);
    expect(deps.client.auction).toHaveBeenCalledTimes(1);
    expect(await screen.findByRole('button', { name: 'View AAPL Moneyline data' })).toBeTruthy();
  });

  it('deep dive navigates with normalized params and never invokes agent or chart APIs', () => {
    const deps = dependencies();
    render(<LensScreen {...deps.props} />);
    fireEvent.press(screen.getByRole('button', { name: 'Select Deep Dive' }));
    fireEvent.press(screen.getByRole('button', { name: 'Start Deep Dive' }));

    expect(deps.router.push).toHaveBeenCalledWith({
      pathname: '/research',
      params: { symbol: 'AAPL', period: '1y', depth: 'deep-dive' },
    });
    expect(deps.client.torque).not.toHaveBeenCalled();
    expect(deps.client.auction).not.toHaveBeenCalled();
    expect(deps.client.moneyline).not.toHaveBeenCalled();
    expect(deps.client).not.toHaveProperty('agentChat');
  });

  it('keeps panels independent, uses honest provenance, and exposes manual Retry', async () => {
    const deps = dependencies();
    deps.client.auction.mockRejectedValueOnce(new Error('Auction provider unavailable')).mockResolvedValueOnce(auction);
    deps.client.moneyline.mockResolvedValue({ ...moneyline, rows: [] });
    render(<LensScreen {...deps.props} />);
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
  ])('keeps one-column reflow and 44-point controls at %ipx / %ix font', (width, fontScale) => {
    const deps = dependencies();
    render(<LensScreen {...deps.props} fontScale={fontScale} width={width} />);
    const action = screen.getByRole('button', { name: 'Open Glance' });
    expect(StyleSheet.flatten(action.props.style).minHeight).toBeGreaterThanOrEqual(44);
    expect(StyleSheet.flatten(screen.getByTestId('lens-content').props.contentContainerStyle)).not.toHaveProperty('height');
    expect(screen.getAllByText(/Glance opens Torque and 5d Auction/).every((text) => text.props.numberOfLines === undefined)).toBe(true);
  });
});
