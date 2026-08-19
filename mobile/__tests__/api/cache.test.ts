import { AsyncCache, CACHE_SCHEMA_VERSION } from '@/src/state/cache';

jest.mock('@react-native-async-storage/async-storage', () => ({
  __esModule: true,
  default: {},
}));

class MemoryStorage {
  readonly values = new Map<string, string>();

  async getItem(key: string) {
    return this.values.get(key) ?? null;
  }

  async setItem(key: string, value: string) {
    this.values.set(key, value);
  }

  async removeItem(key: string) {
    this.values.delete(key);
  }

  async getAllKeys() {
    return [...this.values.keys()];
  }

  async multiRemove(keys: readonly string[]) {
    keys.forEach((key) => this.values.delete(key));
  }
}

describe('AsyncCache', () => {
  it('normalizes endpoint keys and returns schema-versioned fresh records', async () => {
    const storage = new MemoryStorage();
    const cache = new AsyncCache(storage, { now: () => 10_000 });
    await cache.write('/api/watchlists/alerts?b=2&a=1', { rows: [1] });

    const record = await cache.read<{ rows: number[] }>('/api/watchlists/alerts?a=1&b=2');
    expect(record).toMatchObject({
      schemaVersion: CACHE_SCHEMA_VERSION,
      fetchedAt: 10_000,
      data: { rows: [1] },
    });
  });

  it('keys requests by base URL, method, route, and a normalized body', () => {
    const cache = new AsyncCache(new MemoryStorage());
    const left = cache.keyFor({
      baseUrl: 'https://api.test/',
      method: 'post',
      route: '/api/watchlists/alerts',
      body: { period: '1Y', tickers: ['aapl', 'MSFT'], max_alerts: 5 },
    });
    const right = cache.keyFor({
      baseUrl: 'https://api.test',
      method: 'POST',
      route: '/api/watchlists/alerts',
      body: { max_alerts: 5, tickers: ['AAPL', 'msft'], period: '1y' },
    });
    expect(left).toBe(right);
    expect(left).not.toBe(
      cache.keyFor({
        baseUrl: 'https://other.test',
        method: 'POST',
        route: '/api/watchlists/alerts',
        body: { max_alerts: 5, tickers: ['AAPL', 'MSFT'], period: '1y' },
      }),
    );
    expect(left).not.toBe(
      cache.keyFor({
        baseUrl: 'https://api.test',
        method: 'POST',
        route: '/api/watchlists/alerts',
        body: { max_alerts: 5, tickers: ['AAPL', 'MSFT'], period: '5d' },
      }),
    );
  });

  it('removes corruption and records from another schema version', async () => {
    const storage = new MemoryStorage();
    const cache = new AsyncCache(storage);
    const key = cache.keyFor('/api/health');
    storage.values.set(key, '{bad');
    expect(await cache.read('/api/health')).toBeNull();
    expect(storage.values.has(key)).toBe(false);

    storage.values.set(
      key,
      JSON.stringify({ schemaVersion: CACHE_SCHEMA_VERSION - 1, fetchedAt: 1, accessedAt: 1, data: {} }),
    );
    expect(await cache.read('/api/health')).toBeNull();
    expect(storage.values.has(key)).toBe(false);
  });

  it('prunes least-recently-used entries by count and bytes', async () => {
    let now = 1;
    const storage = new MemoryStorage();
    const cache = new AsyncCache(storage, {
      now: () => now++,
      maxEntries: 2,
      maxTotalBytes: 900,
    });
    await cache.write('/one', { payload: 'a'.repeat(100) });
    await cache.write('/two', { payload: 'b'.repeat(100) });
    await cache.read('/one');
    await cache.write('/three', { payload: 'c'.repeat(100) });
    expect(await cache.read('/one')).not.toBeNull();
    expect(await cache.read('/two')).toBeNull();
    expect(await cache.read('/three')).not.toBeNull();
  });

  it('rolls back a newly persisted record when pruning fails', async () => {
    let now = 1;
    const storage = new MemoryStorage();
    const cache = new AsyncCache(storage, { maxEntries: 1, now: () => now++ });
    await cache.write('/existing', { payload: 'keep' });
    storage.multiRemove = async () => {
      throw new Error('prune failed');
    };

    await expect(cache.write('/new', { payload: 'rollback' })).rejects.toThrow('prune failed');
    expect(storage.values.has(cache.keyFor('/new'))).toBe(false);
    expect(storage.values.has(cache.keyFor('/existing'))).toBe(true);
  });

  it('restores the previous record when pruning fails during an overwrite', async () => {
    const storage = new MemoryStorage();
    const cache = new AsyncCache(storage);
    await cache.write('/existing', { payload: 'keep' });
    const key = cache.keyFor('/existing');
    const previous = storage.values.get(key);
    storage.getAllKeys = async () => {
      throw new Error('prune failed');
    };

    await expect(cache.write('/existing', { payload: 'rollback' })).rejects.toThrow('prune failed');
    expect(storage.values.get(key)).toBe(previous);
  });

  it('rejects secrets, base64 artifacts, incomplete streams, and oversized entries', async () => {
    const cache = new AsyncCache(new MemoryStorage(), { maxEntryBytes: 256 });
    await expect(cache.write('/secret', { api_key: 'secret' })).rejects.toThrow(/unsafe/i);
    await expect(cache.write('/artifact', { data: 'data:image/png;base64,AAAA' })).rejects.toThrow(
      /unsafe/i,
    );
    await expect(
      cache.write('/artifact-object', { mime: 'image/png', data: 'AAAA' }),
    ).rejects.toThrow(/unsafe/i);
    await expect(cache.write('/partial', { status: 'streaming', text: 'partial' })).rejects.toThrow(
      /complete/i,
    );
    await expect(cache.write('/large', { payload: 'x'.repeat(400) })).rejects.toThrow(/too large/i);
  });
});
