import { act, fireEvent, render, screen } from '@testing-library/react-native';
import { useState } from 'react';
import { StyleSheet, Text } from 'react-native';
import { Path } from 'react-native-svg';

import ResearchDepthDial, { type ResearchDepth } from '@/src/features/lens/ResearchDepthDial';

function Harness({
  fontScale = 1,
  width = 375,
  haptics = { selectionAsync: jest.fn(async () => undefined) },
}: {
  fontScale?: number;
  width?: number;
  haptics?: { selectionAsync(): Promise<void> };
}) {
  const [depth, setDepth] = useState<ResearchDepth>('glance');
  return (
    <>
      <ResearchDepthDial
        fontScale={fontScale}
        haptics={haptics}
        onChange={setDepth}
        selectedDepth={depth}
        width={width}
      />
      <Text>Harness selected {depth}</Text>
    </>
  );
}

describe('ResearchDepthDial', () => {
  it('renders a semicircular instrument and visible 44-point segmented fallback', () => {
    const view = render(<Harness width={320} />);

    expect(view.UNSAFE_getAllByType(Path).some((path) => String(path.props.d).includes('A'))).toBe(true);
    expect(screen.getByRole('adjustable', { name: 'Research depth' }).props.accessibilityValue.text).toContain('Glance');
    for (const label of ['Glance', 'Diagnose', 'Deep Dive']) {
      const control = screen.getByRole('button', { name: `Select ${label}` });
      expect(StyleSheet.flatten(control.props.style).minHeight).toBeGreaterThanOrEqual(44);
    }
    expect(screen.getByText(/Selected: Glance/)).toBeTruthy();
  });

  it('haptics only fire when a real detent changes', () => {
    const haptics = { selectionAsync: jest.fn(async () => undefined) };
    render(<Harness haptics={haptics} />);

    fireEvent.press(screen.getByRole('button', { name: 'Select Diagnose' }));
    expect(screen.getByText('Harness selected diagnose')).toBeTruthy();
    expect(haptics.selectionAsync).toHaveBeenCalledTimes(1);
    fireEvent.press(screen.getByRole('button', { name: 'Select Diagnose' }));
    expect(haptics.selectionAsync).toHaveBeenCalledTimes(1);
  });

  it('supports equivalent adjustable increment/decrement actions', () => {
    const haptics = { selectionAsync: jest.fn(async () => undefined) };
    render(<Harness haptics={haptics} />);
    const dial = screen.getByRole('adjustable', { name: 'Research depth' });

    fireEvent(dial, 'accessibilityAction', { nativeEvent: { actionName: 'increment' } });
    expect(screen.getByText('Harness selected diagnose')).toBeTruthy();
    fireEvent(screen.getByRole('adjustable'), 'accessibilityAction', { nativeEvent: { actionName: 'increment' } });
    expect(screen.getByText('Harness selected deep-dive')).toBeTruthy();
    fireEvent(screen.getByRole('adjustable'), 'accessibilityAction', { nativeEvent: { actionName: 'increment' } });
    expect(haptics.selectionAsync).toHaveBeenCalledTimes(2);
    fireEvent(screen.getByRole('adjustable'), 'accessibilityAction', { nativeEvent: { actionName: 'decrement' } });
    expect(screen.getByText('Harness selected diagnose')).toBeTruthy();
  });

  it('maps tap and drag releases to stable detents', () => {
    render(<Harness width={300} />);

    fireEvent.press(screen.getByRole('button', { name: 'Select Deep Dive on dial' }));
    expect(screen.getByText('Harness selected deep-dive')).toBeTruthy();
    fireEvent(screen.getByTestId('depth-dial-gesture'), 'responderMove', { nativeEvent: { locationX: 10 } });
    fireEvent(screen.getByTestId('depth-dial-gesture'), 'responderRelease', { nativeEvent: { locationX: 10 } });
    expect(screen.getByText('Harness selected glance')).toBeTruthy();
  });

  it('deduplicates rapid move events within one detent before React rerenders', () => {
    const haptics = { selectionAsync: jest.fn(async () => undefined) };
    render(<Harness haptics={haptics} width={300} />);
    const dial = screen.getByTestId('depth-dial-gesture');
    const event = { nativeEvent: { locationX: 150 } };

    act(() => {
      dial.props.onResponderMove(event);
      dial.props.onResponderMove(event);
    });

    expect(screen.getByText('Harness selected diagnose')).toBeTruthy();
    expect(haptics.selectionAsync).toHaveBeenCalledTimes(1);
  });

  it('reflows the fallback at large font scale without fixed-height text cards', () => {
    render(<Harness fontScale={1.6} width={430} />);
    expect(StyleSheet.flatten(screen.getByTestId('depth-segments').props.style).flexDirection).toBe('column');
    expect(StyleSheet.flatten(screen.getByTestId('depth-feedback').props.style)).not.toHaveProperty('height');
  });
});
