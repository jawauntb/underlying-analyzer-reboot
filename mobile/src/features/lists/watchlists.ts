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

import { normalizeSymbol, normalizeSymbols } from '@/src/api/endpoints';

export const LISTS_STORAGE_KEY = '@undercurrent/lists/v1';
export const LISTS_SCHEMA_VERSION = 1;
export const MAX_LIST_SYMBOLS = 10;

export type SavedListSource =
  | { kind: 'manual' }
  | { kind: 'tradingview'; sourceUrl: string; remoteId: string };

export type SavedList = {
  id: string;
  name: string;
  symbols: string[];
  source: SavedListSource;
  createdAt: number;
  updatedAt: number;
};

export type TradingViewListInput = {
  name: string;
  symbols: readonly string[];
  sourceUrl: string;
  remoteId: string;
};

type ListsEnvelope = {
  schemaVersion: typeof LISTS_SCHEMA_VERSION;
  lists: SavedList[];
};

type ParsedListsEnvelope = {
  envelope: ListsEnvelope;
  corruptListCount: number;
};

export type ListsStorage = Pick<typeof AsyncStorage, 'getItem' | 'setItem' | 'removeItem'>;

type WatchlistStoreOptions = {
  now?: () => number;
  createId?: () => string;
};

type Listener = (lists: readonly SavedList[]) => void;

function defaultId(): string {
  return `list-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 9)}`;
}

function normalizedName(value: string): string {
  const name = value.trim();
  if (!name) throw new Error('List name is required.');
  return name;
}

function symbolTokens(input: string): string[] {
  return input
    .split(/[\s,]+/)
    .map((token) => token.trim())
    .filter(Boolean);
}

export function normalizeListSymbols(input: string | readonly string[]): string[] {
  const values = typeof input === 'string' ? symbolTokens(input) : [...input];
  if (!values.length) throw new Error('Enter at least one symbol.');

  let symbols: string[];
  try {
    symbols = normalizeSymbols(values);
  } catch {
    throw new Error('Invalid symbol. Use 1-15 letters, digits, dots, or hyphens.');
  }
  if (symbols.length > MAX_LIST_SYMBOLS) {
    throw new Error(`A list can contain at most ${MAX_LIST_SYMBOLS} symbols.`);
  }
  return symbols;
}

export type ValidTradingViewUrl = { sourceUrl: string; remoteId: string };

export function validateTradingViewUrl(value: string): ValidTradingViewUrl {
  try {
    const candidate = value.trim();
    const url = new URL(candidate);
    const host = url.hostname.toLowerCase();
    const match = /^\/watchlists\/(\d+)\/$/.exec(url.pathname);
    if (
      !/^https:\/\/(?:www\.)?tradingview\.com\/watchlists\//i.test(candidate) ||
      url.protocol !== 'https:' ||
      (host !== 'tradingview.com' && host !== 'www.tradingview.com') ||
      url.username ||
      url.password ||
      url.port ||
      url.search ||
      url.hash ||
      !match
    ) {
      throw new Error('invalid');
    }
    return { sourceUrl: `https://${host}${url.pathname}`, remoteId: match[1] };
  } catch {
    throw new Error(
      'Enter a public TradingView HTTPS URL like https://www.tradingview.com/watchlists/123/.',
    );
  }
}

function isSavedList(value: unknown): value is SavedList {
  if (typeof value !== 'object' || value === null) return false;
  const list = value as Partial<SavedList>;
  if (
    typeof list.id !== 'string' ||
    !list.id ||
    typeof list.name !== 'string' ||
    !list.name.trim() ||
    !Array.isArray(list.symbols) ||
    typeof list.createdAt !== 'number' ||
    !Number.isFinite(list.createdAt) ||
    typeof list.updatedAt !== 'number' ||
    !Number.isFinite(list.updatedAt) ||
    typeof list.source !== 'object' ||
    list.source === null
  ) {
    return false;
  }
  try {
    const symbols = normalizeListSymbols(list.symbols.map(String));
    if (symbols.length !== list.symbols.length || symbols.some((symbol, index) => symbol !== list.symbols![index])) {
      return false;
    }
  } catch {
    return false;
  }
  const source = list.source as Partial<SavedListSource>;
  if (source.kind === 'manual') return true;
  if (source.kind !== 'tradingview' || typeof source.sourceUrl !== 'string' || typeof source.remoteId !== 'string') {
    return false;
  }
  try {
    const validated = validateTradingViewUrl(source.sourceUrl);
    return validated.remoteId === source.remoteId;
  } catch {
    return false;
  }
}

function parseEnvelope(raw: string): ParsedListsEnvelope | null {
  try {
    const value: unknown = JSON.parse(raw);
    if (typeof value !== 'object' || value === null) return null;
    const envelope = value as Partial<ListsEnvelope>;
    if (envelope.schemaVersion !== LISTS_SCHEMA_VERSION || !Array.isArray(envelope.lists)) return null;
    const lists = envelope.lists.filter(isSavedList);
    return {
      envelope: { schemaVersion: LISTS_SCHEMA_VERSION, lists },
      corruptListCount: envelope.lists.length - lists.length,
    };
  } catch {
    return null;
  }
}

export function newestSavedList(lists: readonly SavedList[]): SavedList | null {
  return lists.reduce<SavedList | null>(
    (newest, list) => (!newest || list.updatedAt >= newest.updatedAt ? list : newest),
    null,
  );
}

export class WatchlistStore {
  private lists: SavedList[] = [];
  private lastDroppedCorruptListCount = 0;
  private readonly listeners = new Set<Listener>();
  private readonly now: () => number;
  private readonly createId: () => string;
  private writeQueue: Promise<void> = Promise.resolve();

  constructor(
    private readonly storage: ListsStorage = AsyncStorage,
    options: WatchlistStoreOptions = {},
  ) {
    this.now = options.now ?? Date.now;
    this.createId = options.createId ?? defaultId;
  }

  snapshot(): SavedList[] {
    return this.lists.map((list) => ({ ...list, symbols: [...list.symbols], source: { ...list.source } }));
  }

  get droppedCorruptListCount(): number {
    return this.lastDroppedCorruptListCount;
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  async hydrate(): Promise<SavedList[]> {
    this.lastDroppedCorruptListCount = 0;
    const raw = await this.storage.getItem(LISTS_STORAGE_KEY);
    if (raw === null) {
      this.replace([]);
      return [];
    }
    const parsed = parseEnvelope(raw);
    if (!parsed) {
      await this.storage.removeItem(LISTS_STORAGE_KEY);
      this.replace([]);
      return [];
    }
    if (parsed.corruptListCount > 0) {
      if (parsed.envelope.lists.length === 0) {
        await this.storage.removeItem(LISTS_STORAGE_KEY);
      } else {
        await this.storage.setItem(LISTS_STORAGE_KEY, JSON.stringify(parsed.envelope));
      }
    }
    this.lastDroppedCorruptListCount = parsed.corruptListCount;
    this.replace(parsed.envelope.lists);
    return this.snapshot();
  }

  saveManual(name: string, symbols: string | readonly string[]): Promise<SavedList> {
    return this.append({ name, symbols, source: { kind: 'manual' } });
  }

  saveTradingView(input: TradingViewListInput): Promise<SavedList> {
    const validated = validateTradingViewUrl(input.sourceUrl);
    if (validated.remoteId !== input.remoteId) throw new Error('TradingView source id does not match its URL.');
    return this.append({
      name: input.name,
      symbols: input.symbols,
      source: { kind: 'tradingview', ...validated },
    });
  }

  private append(input: {
    name: string;
    symbols: string | readonly string[];
    source: SavedListSource;
  }): Promise<SavedList> {
    const name = normalizedName(input.name);
    const symbols = normalizeListSymbols(input.symbols);
    const timestamp = this.now();
    const record: SavedList = {
      id: this.createId(),
      name,
      symbols,
      source: input.source,
      createdAt: timestamp,
      updatedAt: timestamp,
    };
    const operation = this.writeQueue.then(async () => {
      const next = [...this.lists, record];
      const envelope: ListsEnvelope = { schemaVersion: LISTS_SCHEMA_VERSION, lists: next };
      await this.storage.setItem(LISTS_STORAGE_KEY, JSON.stringify(envelope));
      this.replace(next);
    });
    this.writeQueue = operation.catch(() => undefined);
    return operation.then(() => record);
  }

  private replace(lists: readonly SavedList[]): void {
    this.lists = lists.map((list) => ({ ...list, symbols: [...list.symbols], source: { ...list.source } }));
    const snapshot = this.snapshot();
    this.listeners.forEach((listener) => listener(snapshot));
  }
}

export type SavedListsContextValue = {
  hydrated: boolean;
  hydrationError: string | null;
  droppedCorruptListCount: number;
  lists: readonly SavedList[];
  retryHydration(): void;
  saveManual(name: string, symbols: string | readonly string[]): Promise<SavedList>;
  saveTradingView(input: TradingViewListInput): Promise<SavedList>;
};

const unavailable = async (): Promise<never> => {
  throw new Error('Saved lists are not ready.');
};

const SavedListsContext = createContext<SavedListsContextValue>({
  hydrated: false,
  hydrationError: null,
  droppedCorruptListCount: 0,
  lists: [],
  retryHydration: () => undefined,
  saveManual: unavailable,
  saveTradingView: unavailable,
});

const defaultStore = new WatchlistStore();

export function SavedListsProvider({
  children,
  store = defaultStore,
}: PropsWithChildren<{ store?: WatchlistStore }>): ReturnType<typeof createElement> {
  const [lists, setLists] = useState<readonly SavedList[]>(store.snapshot());
  const [hydrated, setHydrated] = useState(false);
  const [hydrationError, setHydrationError] = useState<string | null>(null);
  const [droppedCorruptListCount, setDroppedCorruptListCount] = useState(0);
  const [hydrationAttempt, setHydrationAttempt] = useState(0);
  const retryHydration = useCallback(() => {
    setHydrationAttempt((attempt) => attempt + 1);
  }, []);

  useEffect(() => {
    let mounted = true;
    setHydrated(false);
    setHydrationError(null);
    setDroppedCorruptListCount(0);
    const unsubscribe = store.subscribe((next) => {
      if (mounted) setLists(next);
    });
    void store
      .hydrate()
      .then(() => {
        if (!mounted) return;
        setHydrated(true);
        setHydrationError(null);
        setDroppedCorruptListCount(store.droppedCorruptListCount);
      })
      .catch(() => {
        if (!mounted) return;
        setHydrated(false);
        setHydrationError('Saved lists could not be read from this device. Try again.');
      });
    return () => {
      mounted = false;
      unsubscribe();
    };
  }, [hydrationAttempt, store]);

  const value = useMemo<SavedListsContextValue>(
    () => ({
      hydrated,
      hydrationError,
      droppedCorruptListCount,
      lists,
      retryHydration,
      saveManual: hydrated ? (name, symbols) => store.saveManual(name, symbols) : unavailable,
      saveTradingView: hydrated ? (input) => store.saveTradingView(input) : unavailable,
    }),
    [droppedCorruptListCount, hydrated, hydrationError, lists, retryHydration, store],
  );
  return createElement(SavedListsContext.Provider, { value }, children);
}

export function useSavedLists(): SavedListsContextValue {
  return useContext(SavedListsContext);
}

export function normalizeSavedSymbol(value: string): string {
  return normalizeSymbol(value);
}
