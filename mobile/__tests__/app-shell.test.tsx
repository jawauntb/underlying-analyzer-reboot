import { act, render, screen } from '@testing-library/react-native';
import { AccessibilityInfo, StyleSheet } from 'react-native';

import TabLayout from '@/app/(tabs)/_layout';
import PulseScreen from '@/app/(tabs)/index';
import LibraryScreen from '@/app/(tabs)/library';
import ListsScreen from '@/app/(tabs)/lists';
import RootLayout, { stackAnimationFor } from '@/app/_layout';
import ResearchScreen from '@/app/research';
import TickerScreen from '@/app/ticker/[symbol]';

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
  const Container = ({ children, ...props }: { children?: React.ReactNode }) =>
    React.createElement(View, props, children);

  return { SafeAreaProvider: Container, SafeAreaView: Container };
});

jest.mock('@react-navigation/native', () => {
  const actual = jest.requireActual('@react-navigation/native');
  return { ...actual, useIsFocused: () => true };
});

jest.mock('expo-router', () => {
  const React = jest.requireActual('react');
  const { Text, View } = jest.requireActual('react-native');
  const Navigator = ({ children }: { children?: React.ReactNode }) =>
    React.createElement(View, null, children);
  const stackScreen = ({ name }: { name: string }) =>
    React.createElement(Text, { accessibilityLabel: `stack route ${name}` }, name);
  const tabScreen = ({ name }: { name: string }) =>
    React.createElement(Text, { accessibilityLabel: `tab route ${name}` }, name);

  Navigator.Screen = stackScreen;
  function Tabs({ children }: { children?: React.ReactNode }) {
    return React.createElement(View, null, children);
  }
  Tabs.Screen = tabScreen;

  return {
    Stack: Navigator,
    Tabs,
    useLocalSearchParams: () => ({ symbol: 'AAPL' }),
    useRouter: () => ({ back: jest.fn(), push: jest.fn() }),
  };
});

describe('Undercurrent app shell', () => {
  it.each([
    ['Pulse', PulseScreen],
    ['Lists', ListsScreen],
    ['Library', LibraryScreen],
  ])('renders the %s tab without starting a network request', (name, ScreenComponent) => {
    const fetchSpy = jest.spyOn(globalThis, 'fetch');

    const view = render(<ScreenComponent />);

    expect(screen.getByRole('header', { name })).toBeTruthy();
    expect(fetchSpy).not.toHaveBeenCalled();
    view.unmount();
    fetchSpy.mockRestore();
  });

  it('declares Pulse first and exposes all native routes', async () => {
    const reduceMotionSpy = jest
      .spyOn(AccessibilityInfo, 'isReduceMotionEnabled')
      .mockResolvedValue(true);
    const tabs = render(<TabLayout />);

    expect(tabs.getAllByLabelText(/tab route/).map((route) => route.props.accessibilityLabel)).toEqual([
      'tab route index',
      'tab route lists',
      'tab route library',
    ]);
    tabs.unmount();

    const root = render(<RootLayout />);
    await act(async () => undefined);

    expect(screen.getByLabelText('stack route (tabs)')).toBeTruthy();
    expect(screen.getByLabelText('stack route ticker/[symbol]')).toBeTruthy();
    expect(screen.getByLabelText('stack route research')).toBeTruthy();
    root.unmount();
    reduceMotionSpy.mockRestore();
  });

  it('renders the real Lens and Research destination with 44-point actions', () => {
    const ticker = render(<TickerScreen />);
    const tickerAction = ticker.getByRole('button', { name: 'Open Glance' });

    expect(ticker.getByRole('header', { name: 'AAPL' })).toBeTruthy();
    expect(ticker.getByText(/Opened depth: None/)).toBeTruthy();
    expect(StyleSheet.flatten(tickerAction.props.style).minHeight).toBeGreaterThanOrEqual(44);
    ticker.unmount();

    const research = render(<ResearchScreen />);
    const closeAction = research.getByRole('button', { name: 'Close Research Run preview' });

    expect(research.getByLabelText('Research Run preview placeholder')).toBeTruthy();
    expect(StyleSheet.flatten(closeAction.props.style).minHeight).toBeGreaterThanOrEqual(44);
    research.unmount();
  });

  it('removes native stack animation when Reduce Motion is enabled', () => {
    expect(stackAnimationFor(true)).toBe('none');
    expect(stackAnimationFor(false)).toBe('default');
  });
});
