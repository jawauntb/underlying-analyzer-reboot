import { render, screen } from '@testing-library/react-native';

import E2EFixtureBadge from '@/src/testing/E2EFixtureBadge';

describe('E2EFixtureBadge', () => {
  const previous = process.env.EXPO_PUBLIC_E2E_MODE;

  afterEach(() => {
    process.env.EXPO_PUBLIC_E2E_MODE = previous;
  });

  it('is absent from normal Expo Go and production builds', () => {
    delete process.env.EXPO_PUBLIC_E2E_MODE;
    render(<E2EFixtureBadge />);
    expect(screen.queryByTestId('e2e-fixture-badge')).toBeNull();
  });

  it('stays absent for a truthy lookalike flag', () => {
    process.env.EXPO_PUBLIC_E2E_MODE = 'true';
    render(<E2EFixtureBadge />);
    expect(screen.queryByTestId('e2e-fixture-badge')).toBeNull();
  });

  it('is visible only when the exact fixture flag is one', () => {
    process.env.EXPO_PUBLIC_E2E_MODE = '1';
    render(<E2EFixtureBadge />);
    expect(screen.getByTestId('e2e-fixture-badge')).toHaveTextContent('FIXTURE');
    expect(screen.getByLabelText('E2E fixture mode')).toBeTruthy();
  });
});
