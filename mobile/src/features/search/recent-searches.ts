import AsyncStorage from '@react-native-async-storage/async-storage';

import type { SecuritySearchResult } from '@/src/api/contracts';
import { normalizeSymbol } from '@/src/api/endpoints';
import { isSecurityAssetType } from '@/src/api/guards';

export const RECENT_SEARCHES_STORAGE_KEY = '@undercurrent/recent-searches/v1';
export const RECENT_SEARCHES_SCHEMA_VERSION = 1;
export const MAX_RECENT_SEARCHES = 6;

export type RecentSearchRecord = SecuritySearchResult & {
  selectedAt: number;
};

type RecentSearchEnvelope = {
  schemaVersion: typeof RECENT_SEARCHES_SCHEMA_VERSION;
  records: RecentSearchRecord[];
};

export type RecentSearchStorage = Pick<typeof AsyncStorage, 'getItem' | 'setItem' | 'removeItem'>;
export type RecentSearchStoreApi = Pick<RecentSearchStore, 'hydrate' | 'record'>;

function isBoundedText(value: unknown, maximum: number, allowEmpty = false): value is string {
  return typeof value === 'string' && value.length <= maximum && (allowEmpty || value.trim().length > 0);
}

function validatedRecord(value: unknown): RecentSearchRecord | null {
  if (typeof value !== 'object' || value === null) return null;
  const record = value as Partial<RecentSearchRecord>;
  if (
    !isBoundedText(record.symbol, 15)
    || !isBoundedText(record.name, 200, true)
    || !isBoundedText(record.exchange, 80, true)
    || !isSecurityAssetType(record.assetType)
    || typeof record.selectedAt !== 'number'
    || !Number.isFinite(record.selectedAt)
    || record.selectedAt < 0
  ) return null;

  try {
    const symbol = normalizeSymbol(record.symbol);
    if (symbol !== record.symbol) return null;
    return {
      symbol,
      name: record.name.trim(),
      exchange: record.exchange.trim(),
      assetType: record.assetType,
      selectedAt: record.selectedAt,
    };
  } catch {
    return null;
  }
}

function parseEnvelope(raw: string): { records: RecentSearchRecord[]; repaired: boolean } | null {
  try {
    const value: unknown = JSON.parse(raw);
    if (typeof value !== 'object' || value === null) return null;
    const envelope = value as Partial<RecentSearchEnvelope>;
    if (envelope.schemaVersion !== RECENT_SEARCHES_SCHEMA_VERSION || !Array.isArray(envelope.records)) {
      return null;
    }
    const records = envelope.records
      .map(validatedRecord)
      .filter((record): record is RecentSearchRecord => record !== null)
      .sort((left, right) => right.selectedAt - left.selectedAt)
      .filter((record, index, all) => all.findIndex((candidate) => candidate.symbol === record.symbol) === index)
      .slice(0, MAX_RECENT_SEARCHES);
    return { records, repaired: records.length !== envelope.records.length };
  } catch {
    return null;
  }
}

export class RecentSearchStore {
  private records: RecentSearchRecord[] = [];
  private hydrationInFlight: Promise<RecentSearchRecord[]> | null = null;
  private writeQueue: Promise<void> = Promise.resolve();
  private readonly now: () => number;

  constructor(
    private readonly storage: RecentSearchStorage = AsyncStorage,
    options: { now?: () => number } = {},
  ) {
    this.now = options.now ?? Date.now;
  }

  snapshot(): RecentSearchRecord[] {
    return this.records.map((record) => ({ ...record }));
  }

  hydrate(): Promise<RecentSearchRecord[]> {
    const operation = this.load();
    this.hydrationInFlight = operation;
    void operation.finally(() => {
      if (this.hydrationInFlight === operation) this.hydrationInFlight = null;
    }).catch(() => undefined);
    return operation;
  }

  private async load(): Promise<RecentSearchRecord[]> {
    const raw = await this.storage.getItem(RECENT_SEARCHES_STORAGE_KEY);
    if (raw === null) {
      this.records = [];
      return [];
    }
    const parsed = parseEnvelope(raw);
    if (!parsed) {
      await this.storage.removeItem(RECENT_SEARCHES_STORAGE_KEY);
      this.records = [];
      return [];
    }
    this.records = parsed.records;
    if (parsed.repaired) await this.persist(parsed.records);
    return this.snapshot();
  }

  record(result: SecuritySearchResult): Promise<RecentSearchRecord[]> {
    const hydration = this.hydrationInFlight ?? Promise.resolve(this.snapshot());
    return hydration.catch(() => []).then(async () => {
      let symbol: string;
      try {
        symbol = normalizeSymbol(result.symbol);
      } catch {
        throw new Error('Recent search result is invalid.');
      }
      const candidate = validatedRecord({ ...result, symbol, selectedAt: this.now() });
      if (!candidate) throw new Error('Recent search result is invalid.');
      const next = [candidate, ...this.records.filter((record) => record.symbol !== candidate.symbol)]
        .slice(0, MAX_RECENT_SEARCHES);
      this.records = next;
      await this.persist(next);
      return this.snapshot();
    });
  }

  private persist(records: readonly RecentSearchRecord[]): Promise<void> {
    const envelope: RecentSearchEnvelope = {
      schemaVersion: RECENT_SEARCHES_SCHEMA_VERSION,
      records: records.map((record) => ({ ...record })),
    };
    const operation = this.writeQueue.then(() =>
      this.storage.setItem(RECENT_SEARCHES_STORAGE_KEY, JSON.stringify(envelope)),
    );
    this.writeQueue = operation.catch(() => undefined);
    return operation;
  }
}
