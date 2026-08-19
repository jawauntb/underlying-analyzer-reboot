import { fireEvent, render, screen } from '@testing-library/react-native';
import { StyleSheet } from 'react-native';
import { Path } from 'react-native-svg';

import { AuctionChart } from '@/src/components/charts/AuctionChart';
import { LineChart } from '@/src/components/charts/LineChart';
import { MoneylineChart } from '@/src/components/charts/MoneylineChart';
import { TorqueChart } from '@/src/components/charts/TorqueChart';

function finiteProps(value: unknown): boolean {
  if (typeof value === 'number') return Number.isFinite(value);
  if (typeof value === 'string') return !/NaN|Infinity/.test(value);
  if (Array.isArray(value)) return value.every(finiteProps);
  if (value && typeof value === 'object') return Object.values(value).every(finiteProps);
  return true;
}

describe('LineChart accessibility and rendering', () => {
  const lines = [
    {
      key: 'close',
      label: 'Close — solid',
      color: '#fff',
      points: [
        { date: 'Mon', categoryIndex: 0, value: 10 },
        { date: 'Tue', categoryIndex: 1, value: -2 },
        { date: 'Wed', categoryIndex: 2, value: 14 },
      ],
    },
    {
      key: 'average',
      label: 'Average – dashed',
      color: '#aaa',
      dashArray: '6 4',
      points: [
        { date: 'Mon', categoryIndex: 0, value: 9 },
        { date: 'Wed', categoryIndex: 2, value: 11 },
      ],
    },
  ];

  it('is one adjustable element with clamped traversal and an activation action', () => {
    render(<LineChart lines={lines} title="Price history" width={320} />);
    const chart = screen.getByRole('adjustable', { name: /Price history chart/ });

    expect(chart.props.accessibilityActions.map((action: { name: string }) => action.name)).toEqual([
      'increment',
      'decrement',
      'activate',
    ]);
    expect(chart.props.accessibilityValue.text).toContain('Mon');

    fireEvent(chart, 'accessibilityAction', { nativeEvent: { actionName: 'decrement' } });
    expect(screen.getByRole('adjustable').props.accessibilityValue.text).toContain('Mon');
    fireEvent(screen.getByRole('adjustable'), 'accessibilityAction', { nativeEvent: { actionName: 'increment' } });
    expect(screen.getByRole('adjustable').props.accessibilityValue.text).toContain('Tue');
    fireEvent(screen.getByRole('adjustable'), 'accessibilityAction', { nativeEvent: { actionName: 'activate' } });
    expect(screen.getByRole('header', { name: 'Price history data' })).toBeTruthy();
  });

  it('shows all normalized values through a visible 44-point data button', () => {
    const view = render(<LineChart lines={lines} title="Price history" width={375} />);
    const button = view.getByRole('button', { name: 'View Price history data' });

    expect(StyleSheet.flatten(button.props.style).minHeight).toBeGreaterThanOrEqual(44);
    fireEvent.press(button);
    expect(view.getByText('Mon')).toBeTruthy();
    expect(view.getByText('Wed')).toBeTruthy();
    expect(view.getByText('14')).toBeTruthy();
    expect(view.getByLabelText('Mon. Close — solid 10, Average – dashed 9').props.accessible).toBe(true);
  });

  it('uses native labels and non-color line styles within the SVG node budget', () => {
    const view = render(<LineChart lines={lines} title="Price history" width={430} />);
    const paths = view.UNSAFE_getAllByType(Path);
    const plot = view.getByTestId('Price history-plot', { includeHiddenElements: true });

    expect(view.getByText('Close — solid')).toBeTruthy();
    expect(view.getByText('Average – dashed')).toBeTruthy();
    expect(paths.some((path) => path.props.strokeDasharray === '6 4')).toBe(true);
    expect(paths.length).toBeLessThanOrEqual(16);
    expect(plot.props.importantForAccessibility).toBe('no-hide-descendants');
    expect(paths.every((path) => finiteProps(path.props))).toBe(true);
    expect(view.getByText('Mon', { includeHiddenElements: true }).props.numberOfLines).toBeUndefined();
  });

  it('shows an explicit unavailable state for empty data', () => {
    render(<LineChart lines={[]} title="Empty history" width={320} />);

    expect(screen.getByText('Chart data unavailable.')).toBeTruthy();
    expect(screen.getByRole('adjustable').props.accessibilityState.disabled).toBe(true);
  });
});

describe('financial chart surfaces', () => {
  const auction = {
    levels: { vah: 14, val: 8, poc: 11 },
    series: {
      ohlcv: [
        { date: '2026-08-14', open: 10, high: 12, low: 9, close: 11, volume: 2 },
        { date: '2026-08-17', open: 11, high: 14, low: 8, close: 13, volume: 3 },
      ],
    },
  };

  it('renders auction candles and patterned levels as one accessible plot', () => {
    const view = render(<AuctionChart dataset={auction} title="AAPL auction" width={320} />);
    const paths = view.UNSAFE_getAllByType(Path);

    expect(view.getByText('VAH — dashed')).toBeTruthy();
    expect(view.getByText('VAL — dashed')).toBeTruthy();
    expect(view.getByText('POC — dash-dot')).toBeTruthy();
    expect(paths.length).toBeLessThanOrEqual(20);
    expect(paths.every((path) => finiteProps(path.props))).toBe(true);
  });

  it('hides absent auction levels instead of showing empty legend claims', () => {
    const view = render(<AuctionChart dataset={{ series: { ohlcv: auction.series.ohlcv } }} title="Auction no levels" width={320} />);

    expect(view.queryByText('VAH — dashed')).toBeNull();
    expect(view.queryByText('VAL — dashed')).toBeNull();
    expect(view.queryByText('POC — dash-dot')).toBeNull();
  });

  it('keeps auction unavailable when levels exist without candles', () => {
    const view = render(<AuctionChart dataset={{ levels: { vah: 14 } }} title="Auction levels only" width={320} />);

    expect(view.getByText('Chart data unavailable.')).toBeTruthy();
    expect(view.getByRole('button', { name: 'View Auction levels only data' })).toBeTruthy();
  });

  it('renders one-point auction data and exposes its full source row', () => {
    const one = { series: { ohlcv: [auction.series.ohlcv[0]] } };
    const view = render(<AuctionChart dataset={one} title="One auction" width={375} />);

    fireEvent.press(view.getByRole('button', { name: 'View One auction data' }));
    expect(view.getByText('2026-08-14')).toBeTruthy();
    expect(view.getByText('O 10 · H 12 · L 9 · C 11 · V 2')).toBeTruthy();
  });

  it('renders all-zero moneyline as unavailable and negative net OI as data', () => {
    const zero = render(
      <MoneylineChart
        dataset={{ series: { strikes: [{ strike: 100, call_open_interest: 0, put_open_interest: 0 }] } }}
        title="Zero moneyline"
        width={320}
      />,
    );
    expect(zero.getByText('Options positioning is unavailable.')).toBeTruthy();
    zero.unmount();

    const negative = render(
      <MoneylineChart
        dataset={{
          series: {
            strikes: [
              { strike: 100, call_open_interest: 2, put_open_interest: 5, net_open_interest: -3 },
            ],
          },
        }}
        title="AAPL moneyline"
        width={375}
      />,
    );
    expect(negative.UNSAFE_getAllByType(Path).length).toBeLessThanOrEqual(10);
    fireEvent.press(negative.getByRole('button', { name: 'View AAPL moneyline data' }));
    expect(negative.getByText(/Net -3/)).toBeTruthy();
  });

  it('hides the moneyline spot legend when current price is absent', () => {
    const view = render(
      <MoneylineChart
        dataset={{ series: { strikes: [{ strike: 100, call_open_interest: 2, put_open_interest: 5 }] } }}
        title="Moneyline no spot"
        width={375}
      />,
    );

    expect(view.queryByText('Spot — dashed')).toBeNull();
  });

  it('renders torque overlays without a base line and a technical-only fundamentals state', () => {
    const view = render(
      <TorqueChart
        dataset={{
          series: {
            price: { ema75: [{ date: 'd1', value: 9 }], sma200: [{ date: 'd1', value: 8 }] },
            fundamentals: { revenue: [], gross_margin: [], operating_margin: [] },
          },
        }}
        title="AAPL torque"
        width={430}
      />,
    );

    expect(view.getByText('EMA 75 — dashed')).toBeTruthy();
    expect(view.getByText('SMA 200 — dotted')).toBeTruthy();
    expect(view.getByText('Fundamental data unavailable — technicals only.')).toBeTruthy();
    expect(view.UNSAFE_getAllByType(Path).length).toBeLessThanOrEqual(35);
  });

  it('shows technical price unavailable while retaining fundamental data access', () => {
    const view = render(
      <TorqueChart
        dataset={{
          series: {
            price: {},
            fundamentals: { revenue: [{ label: 'Q1', value: 100 }] },
          },
        }}
        title="Fundamental torque"
        width={375}
      />,
    );

    expect(view.getByText('Technical price data unavailable.')).toBeTruthy();
    fireEvent.press(view.getByRole('button', { name: 'View Fundamental torque data' }));
    expect(view.getByText('100')).toBeTruthy();
  });

  it.each([320, 375, 430])('stacks chart legends at compact widths and preserves %i-point structure', (width) => {
    const view = render(<AuctionChart dataset={auction} title={`Auction ${width}`} width={width} fontScale={width === 430 ? 1.3 : 1} />);
    const legend = view.getByTestId(`Auction ${width}-legend`);
    const expectedDirection = width < 350 || width === 430 ? 'column' : 'row';

    expect(StyleSheet.flatten(legend.props.style).flexDirection).toBe(expectedDirection);
    expect(view.getByRole('button', { name: `View Auction ${width} data` })).toBeTruthy();
  });
});
