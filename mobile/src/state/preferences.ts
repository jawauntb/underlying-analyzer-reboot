import AsyncStorage from '@react-native-async-storage/async-storage';
import {
  createContext,
  createElement,
  type PropsWithChildren,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from 'react';

import { CHART_INTERVALS, RESEARCH_DEPTHS, type ChartInterval, type ResearchDepth } from '@/src/features/lens/lens-model';

export const PREFERENCES_STORAGE_KEY = '@undercurrent/preferences/v1';
export const PREFERENCES_SCHEMA_VERSION = 1;

export type Preferences = {
  /** Interval every chart opens on. */
  defaultInterval: ChartInterval;
  /** Research depth the Lens preselects. */
  defaultDepth: ResearchDepth;
  /** Whether the Lens spends a request on the live quote snapshot. */
  liveQuotes: boolean;
};

export const DEFAULT_PREFERENCES: Preferences = {
  defaultInterval: '1d',
  defaultDepth: 'glance',
  liveQuotes: true,
};

type PreferencesEnvelope = {
  schemaVersion: typeof PREFERENCES_SCHEMA_VERSION;
  preferences: Preferences;
};

export type PreferencesStorage = Pick<typeof AsyncStorage, 'getItem' | 'setItem' | 'removeItem'>;

type Listener = (preferences: Preferences) => void;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Unknown or corrupt fields fall back to the default rather than failing the read. */
export function parsePreferences(raw: string | null): Preferences {
  if (raw === null) return { ...DEFAULT_PREFERENCES };
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    return { ...DEFAULT_PREFERENCES };
  }
  if (!isRecord(value) || value.schemaVersion !== PREFERENCES_SCHEMA_VERSION || !isRecord(value.preferences)) {
    return { ...DEFAULT_PREFERENCES };
  }
  const stored = value.preferences;
  return {
    defaultInterval: CHART_INTERVALS.includes(stored.defaultInterval as ChartInterval)
      ? stored.defaultInterval as ChartInterval
      : DEFAULT_PREFERENCES.defaultInterval,
    defaultDepth: RESEARCH_DEPTHS.includes(stored.defaultDepth as ResearchDepth)
      ? stored.defaultDepth as ResearchDepth
      : DEFAULT_PREFERENCES.defaultDepth,
    liveQuotes: typeof stored.liveQuotes === 'boolean' ? stored.liveQuotes : DEFAULT_PREFERENCES.liveQuotes,
  };
}

export class PreferencesStore {
  private preferences: Preferences = { ...DEFAULT_PREFERENCES };
  private readonly listeners = new Set<Listener>();
  private writeQueue: Promise<void> = Promise.resolve();

  constructor(private readonly storage: PreferencesStorage = AsyncStorage) {}

  snapshot(): Preferences {
    return { ...this.preferences };
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  async hydrate(): Promise<Preferences> {
    this.replace(parsePreferences(await this.storage.getItem(PREFERENCES_STORAGE_KEY)));
    return this.snapshot();
  }

  /** Persist a patch, keeping memory unchanged if the durable write fails. */
  update(patch: Partial<Preferences>): Promise<Preferences> {
    const next: Preferences = { ...this.preferences, ...patch };
    const operation = this.writeQueue.then(async () => {
      const envelope: PreferencesEnvelope = { schemaVersion: PREFERENCES_SCHEMA_VERSION, preferences: next };
      await this.storage.setItem(PREFERENCES_STORAGE_KEY, JSON.stringify(envelope));
      this.replace(next);
    });
    this.writeQueue = operation.catch(() => undefined);
    return operation.then(() => this.snapshot());
  }

  async reset(): Promise<Preferences> {
    await this.storage.removeItem(PREFERENCES_STORAGE_KEY);
    this.replace({ ...DEFAULT_PREFERENCES });
    return this.snapshot();
  }

  private replace(preferences: Preferences): void {
    this.preferences = { ...preferences };
    const snapshot = this.snapshot();
    this.listeners.forEach((listener) => listener(snapshot));
  }
}

export type PreferencesContextValue = {
  hydrated: boolean;
  error: string | null;
  preferences: Preferences;
  update(patch: Partial<Preferences>): Promise<Preferences>;
  reset(): Promise<Preferences>;
};

const unavailable = async (): Promise<never> => {
  throw new Error('Settings are not ready.');
};

const PreferencesContext = createContext<PreferencesContextValue>({
  hydrated: false,
  error: null,
  preferences: DEFAULT_PREFERENCES,
  update: unavailable,
  reset: unavailable,
});

const defaultStore = new PreferencesStore();

export function PreferencesProvider({
  children,
  store = defaultStore,
}: PropsWithChildren<{ store?: PreferencesStore }>): ReturnType<typeof createElement> {
  const [preferences, setPreferences] = useState<Preferences>(store.snapshot());
  const [hydrated, setHydrated] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    const unsubscribe = store.subscribe((next) => {
      if (mounted) setPreferences(next);
    });
    void store
      .hydrate()
      .then(() => mounted && setHydrated(true))
      .catch(() => {
        if (!mounted) return;
        setHydrated(true);
        setError('Saved settings could not be read. Defaults are in use until the next change sticks.');
      });
    return () => {
      mounted = false;
      unsubscribe();
    };
  }, [store]);

  const update = useCallback((patch: Partial<Preferences>) => store.update(patch), [store]);
  const reset = useCallback(() => store.reset(), [store]);
  const value = useMemo<PreferencesContextValue>(
    () => ({ hydrated, error, preferences, update, reset }),
    [error, hydrated, preferences, reset, update],
  );
  return createElement(PreferencesContext.Provider, { value }, children);
}

export function usePreferences(): PreferencesContextValue {
  return useContext(PreferencesContext);
}
