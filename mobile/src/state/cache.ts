import AsyncStorage from '@react-native-async-storage/async-storage';

export const CACHE_SCHEMA_VERSION = 1;
export const CACHE_NAMESPACE = '@undercurrent/cache/v1/';
export const TTL_MS = {
  capability: 15 * 60 * 1000,
  pulse: 60 * 1000,
  charts: 5 * 60 * 1000,
} as const;

export type CacheRecord<T> = {
  schemaVersion: typeof CACHE_SCHEMA_VERSION;
  fetchedAt: number;
  accessedAt: number;
  data: T;
};

export type AsyncStorageLike = {
  getItem(key: string): Promise<string | null>;
  setItem(key: string, value: string): Promise<void>;
  removeItem(key: string): Promise<void>;
  getAllKeys(): Promise<readonly string[]>;
  multiRemove(keys: readonly string[]): Promise<void>;
};

export type CacheRequestDescriptor = {
  baseUrl: string;
  method: string;
  route: string;
  body?: unknown;
};

type CacheOptions = {
  now?: () => number;
  maxEntries?: number;
  maxEntryBytes?: number;
  maxTotalBytes?: number;
};

const DEFAULT_LIMITS = {
  maxEntries: 48,
  maxEntryBytes: 128 * 1024,
  maxTotalBytes: 3 * 1024 * 1024,
};

export function utf8Bytes(value: string): number {
  return new TextEncoder().encode(value).length;
}

function normalizeEndpointKey(endpoint: string): string {
  const url = new URL(endpoint, 'https://cache.undercurrent.invalid');
  const params: [string, string][] = [];
  url.searchParams.forEach((value, key) => params.push([key, value]));
  const sorted = params.sort(([leftKey, leftValue], [rightKey, rightValue]) =>
    leftKey === rightKey ? leftValue.localeCompare(rightValue) : leftKey.localeCompare(rightKey),
  );
  url.search = '';
  sorted.forEach(([key, value]) => url.searchParams.append(key, value));
  return `${url.pathname}${url.search}`;
}

function normalizeCacheValue(value: unknown, key = ''): unknown {
  if (Array.isArray(value)) {
    if (key === 'tickers') {
      const seen = new Set<string>();
      return value.flatMap((ticker) => {
        const normalized = String(ticker).trim().toUpperCase();
        if (!normalized || seen.has(normalized)) return [];
        seen.add(normalized);
        return [normalized];
      });
    }
    return value.map((child) => normalizeCacheValue(child));
  }
  if (typeof value !== 'object' || value === null) {
    if (key === 'ticker' && typeof value === 'string') return value.trim().toUpperCase();
    if ((key === 'period' || key === 'interval') && typeof value === 'string') return value.trim().toLowerCase();
    return value;
  }
  return Object.fromEntries(
    Object.keys(value)
      .sort()
      .map((childKey) => [childKey, normalizeCacheValue((value as Record<string, unknown>)[childKey], childKey)]),
  );
}

function descriptorKey(descriptor: CacheRequestDescriptor): string {
  const baseUrl = descriptor.baseUrl.trim().replace(/\/+$/, '');
  const route = normalizeEndpointKey(descriptor.route);
  const body = descriptor.body === undefined ? '' : JSON.stringify(normalizeCacheValue(descriptor.body));
  return `${descriptor.method.trim().toUpperCase()} ${baseUrl}${route}${body ? ` ${body}` : ''}`;
}

function assertSafeForPersistence(value: unknown, path = 'record'): void {
  if (typeof value === 'string') {
    if (/^data:[^;]+;base64,/i.test(value)) throw new Error(`Unsafe base64 data at ${path}.`);
    return;
  }
  if (Array.isArray(value)) {
    value.forEach((child, index) => assertSafeForPersistence(child, `${path}[${index}]`));
    return;
  }
  if (typeof value !== 'object' || value === null) return;
  const record = value as Record<string, unknown>;
  if (
    typeof record.mime === 'string' &&
    /^(?:image|audio|video)\//i.test(record.mime) &&
    typeof record.data === 'string'
  ) {
    throw new Error(`Unsafe binary artifact at ${path}.data.`);
  }
  for (const [key, child] of Object.entries(value)) {
    if (/(api[_-]?key|secret|password|authorization|access[_-]?token|service[_-]?role)/i.test(key)) {
      throw new Error(`Unsafe credential field at ${path}.${key}.`);
    }
    if (key === 'status' && ['streaming', 'cancelled'].includes(String(child))) {
      throw new Error('Only complete records may be cached.');
    }
    if (['base64', 'bytes'].includes(key.toLowerCase())) {
      throw new Error(`Unsafe artifact field at ${path}.${key}.`);
    }
    assertSafeForPersistence(child, `${path}.${key}`);
  }
}

function parseRecord<T>(raw: string): CacheRecord<T> | null {
  try {
    const value: unknown = JSON.parse(raw);
    if (
      typeof value !== 'object' ||
      value === null ||
      (value as Partial<CacheRecord<T>>).schemaVersion !== CACHE_SCHEMA_VERSION ||
      typeof (value as Partial<CacheRecord<T>>).fetchedAt !== 'number' ||
      typeof (value as Partial<CacheRecord<T>>).accessedAt !== 'number' ||
      !Object.prototype.hasOwnProperty.call(value, 'data')
    ) {
      return null;
    }
    return value as CacheRecord<T>;
  } catch {
    return null;
  }
}

export class AsyncCache {
  private readonly now: () => number;
  private readonly limits: typeof DEFAULT_LIMITS;

  constructor(
    private readonly storage: AsyncStorageLike = AsyncStorage,
    options: CacheOptions = {},
  ) {
    this.now = options.now ?? Date.now;
    this.limits = {
      maxEntries: options.maxEntries ?? DEFAULT_LIMITS.maxEntries,
      maxEntryBytes: options.maxEntryBytes ?? DEFAULT_LIMITS.maxEntryBytes,
      maxTotalBytes: options.maxTotalBytes ?? DEFAULT_LIMITS.maxTotalBytes,
    };
  }

  keyFor(endpoint: string | CacheRequestDescriptor): string {
    const identity = typeof endpoint === 'string' ? normalizeEndpointKey(endpoint) : descriptorKey(endpoint);
    return `${CACHE_NAMESPACE}${encodeURIComponent(identity)}`;
  }

  async read<T>(endpoint: string | CacheRequestDescriptor): Promise<CacheRecord<T> | null> {
    const key = this.keyFor(endpoint);
    const raw = await this.storage.getItem(key);
    if (raw === null) return null;
    const record = parseRecord<T>(raw);
    if (!record) {
      await this.storage.removeItem(key);
      return null;
    }
    const touched = { ...record, accessedAt: this.now() };
    await this.storage.setItem(key, JSON.stringify(touched));
    return touched;
  }

  async write<T>(endpoint: string | CacheRequestDescriptor, data: T, fetchedAt = this.now()): Promise<CacheRecord<T>> {
    assertSafeForPersistence(data);
    const record: CacheRecord<T> = {
      schemaVersion: CACHE_SCHEMA_VERSION,
      fetchedAt,
      accessedAt: this.now(),
      data,
    };
    const serialized = JSON.stringify(record);
    if (utf8Bytes(serialized) > this.limits.maxEntryBytes) {
      throw new Error('Cache record is too large.');
    }
    const key = this.keyFor(endpoint);
    const previous = await this.storage.getItem(key);
    await this.storage.setItem(key, serialized);
    try {
      await this.prune();
    } catch (error) {
      try {
        if (previous === null) await this.storage.removeItem(key);
        else await this.storage.setItem(key, previous);
      } catch {
        // Preserve the prune failure that triggered the rollback attempt.
      }
      throw error;
    }
    return record;
  }

  async remove(endpoint: string | CacheRequestDescriptor): Promise<void> {
    await this.storage.removeItem(this.keyFor(endpoint));
  }

  async clear(): Promise<void> {
    const keys = (await this.storage.getAllKeys()).filter((key) => key.startsWith(CACHE_NAMESPACE));
    if (keys.length) await this.storage.multiRemove(keys);
  }

  private async prune(): Promise<void> {
    const keys = (await this.storage.getAllKeys()).filter((key) => key.startsWith(CACHE_NAMESPACE));
    const entries: { key: string; bytes: number; accessedAt: number }[] = [];
    const invalid: string[] = [];
    for (const key of keys) {
      const raw = await this.storage.getItem(key);
      const record = raw === null ? null : parseRecord(raw);
      if (!raw || !record) {
        invalid.push(key);
        continue;
      }
      entries.push({ key, bytes: utf8Bytes(raw), accessedAt: record.accessedAt });
    }
    if (invalid.length) await this.storage.multiRemove(invalid);
    entries.sort((left, right) => right.accessedAt - left.accessedAt);
    let totalBytes = 0;
    const remove: string[] = [];
    entries.forEach((entry, index) => {
      totalBytes += entry.bytes;
      if (index >= this.limits.maxEntries || totalBytes > this.limits.maxTotalBytes) remove.push(entry.key);
    });
    if (remove.length) await this.storage.multiRemove(remove);
  }
}
