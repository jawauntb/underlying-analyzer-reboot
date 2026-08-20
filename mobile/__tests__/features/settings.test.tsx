import { fireEvent, render, screen, waitFor } from '@testing-library/react-native';
import React from 'react';

import SettingsScreen from '@/src/features/settings/SettingsScreen';
import {
  DEFAULT_PREFERENCES,
  parsePreferences,
  PREFERENCES_STORAGE_KEY,
  PreferencesStore,
  type PreferencesContextValue,
} from '@/src/state/preferences';

jest.mock('react-native-safe-area-context', () => {
  const React = jest.requireActual('react');
  const { View } = jest.requireActual('react-native');
  return { SafeAreaView: ({ children, ...props }: { children?: React.ReactNode }) => React.createElement(View, props, children) };
});

class MemoryStorage {
  readonly values = new Map<string, string>();
  failWrites = false;

  async getItem(key: string) {
    return this.values.get(key) ?? null;
  }

  async setItem(key: string, value: string) {
    if (this.failWrites) throw new Error('Disk is full.');
    this.values.set(key, value);
  }

  async removeItem(key: string) {
    this.values.delete(key);
  }
}

function dependencies(overrides: Partial<PreferencesContextValue> = {}) {
  const preferencesState: PreferencesContextValue = {
    hydrated: true,
    error: null,
    preferences: { ...DEFAULT_PREFERENCES },
    update: jest.fn(async (patch) => ({ ...DEFAULT_PREFERENCES, ...patch })),
    reset: jest.fn(async () => ({ ...DEFAULT_PREFERENCES })),
    ...overrides,
  };
  const cache = { clear: jest.fn(async () => undefined) };
  return { cache, preferencesState, props: { cache: cache as never, preferencesState, reachability: 'online' as const, version: '1.2.3' } };
}

describe('preferences storage', () => {
  it('falls back to defaults for missing, corrupt, and unknown values', () => {
    expect(parsePreferences(null)).toEqual(DEFAULT_PREFERENCES);
    expect(parsePreferences('not json')).toEqual(DEFAULT_PREFERENCES);
    expect(parsePreferences(JSON.stringify({ schemaVersion: 99, preferences: { defaultInterval: '1w' } }))).toEqual(DEFAULT_PREFERENCES);
    expect(
      parsePreferences(JSON.stringify({
        schemaVersion: 1,
        preferences: { defaultInterval: '4h', defaultDepth: 'diagnose', liveQuotes: 'yes' },
      })),
    ).toEqual({ defaultInterval: '1d', defaultDepth: 'diagnose', liveQuotes: true });
  });

  it('persists a patch and keeps memory unchanged when the write fails', async () => {
    const storage = new MemoryStorage();
    const store = new PreferencesStore(storage);
    await store.hydrate();

    expect(await store.update({ defaultInterval: '15m' })).toMatchObject({ defaultInterval: '15m', liveQuotes: true });
    expect(JSON.parse(storage.values.get(PREFERENCES_STORAGE_KEY)!)).toMatchObject({
      schemaVersion: 1,
      preferences: { defaultInterval: '15m' },
    });

    storage.failWrites = true;
    await expect(store.update({ liveQuotes: false })).rejects.toThrow('Disk is full.');
    expect(store.snapshot()).toMatchObject({ defaultInterval: '15m', liveQuotes: true });

    storage.failWrites = false;
    await store.reset();
    expect(store.snapshot()).toEqual(DEFAULT_PREFERENCES);
    expect(storage.values.has(PREFERENCES_STORAGE_KEY)).toBe(false);
  });
});

describe('SettingsScreen', () => {
  it('saves each default and reports what was cleared', async () => {
    const deps = dependencies();
    render(<SettingsScreen {...deps.props} />);

    fireEvent.press(screen.getByRole('tab', { name: 'Open charts on the weekly interval' }));
    await waitFor(() => expect(deps.preferencesState.update).toHaveBeenCalledWith({ defaultInterval: '1w' }));

    fireEvent.press(screen.getByRole('tab', { name: 'Preselect Diagnose' }));
    await waitFor(() => expect(deps.preferencesState.update).toHaveBeenCalledWith({ defaultDepth: 'diagnose' }));

    fireEvent(screen.getByLabelText('Live quote card'), 'valueChange', false);
    await waitFor(() => expect(deps.preferencesState.update).toHaveBeenCalledWith({ liveQuotes: false }));

    fireEvent.press(screen.getByRole('button', { name: 'Clear saved data' }));
    await waitFor(() => expect(deps.cache.clear).toHaveBeenCalled());
    expect(await screen.findByText(/Saved charts and research were cleared/)).toBeTruthy();
  });

  it('surfaces a failed save and a failed clear instead of implying success', async () => {
    const deps = dependencies({ update: jest.fn(async () => { throw new Error('Disk is full.'); }) });
    deps.cache.clear.mockRejectedValueOnce(new Error('Saved data is locked.'));
    render(<SettingsScreen {...deps.props} />);

    fireEvent(screen.getByLabelText('Live quote card'), 'valueChange', false);
    expect(await screen.findByText('Disk is full.')).toBeTruthy();

    fireEvent.press(screen.getByRole('button', { name: 'Clear saved data' }));
    expect(await screen.findByText('Saved data is locked.')).toBeTruthy();
  });

  it('reports honest diagnostics for this build', () => {
    const deps = dependencies();
    render(<SettingsScreen {...deps.props} reachability="offline" />);

    expect(screen.getByLabelText('CONNECTION: Offline · saved data only')).toBeTruthy();
    expect(screen.getByLabelText('APP VERSION: 1.2.3')).toBeTruthy();
  });
});
