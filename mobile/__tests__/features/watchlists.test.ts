import {
  LISTS_STORAGE_KEY,
  WatchlistStore,
  newestSavedList,
  normalizeListSymbols,
  validateTradingViewUrl,
} from '@/src/features/lists/watchlists';

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
}

describe('saved watchlists', () => {
  it('normalizes stable uppercase symbols and enforces the post-dedupe limit', () => {
    expect(normalizeListSymbols(' msft, aapl MSFT\nBRK.b ')).toEqual(['MSFT', 'AAPL', 'BRK.B']);
    expect(() => normalizeListSymbols('AAPL, BAD/SYMBOL')).toThrow(/invalid symbol/i);
    expect(() => normalizeListSymbols('A B C D E F G H I J J K')).toThrow(/10 symbols/i);
    expect(() => normalizeListSymbols('')).toThrow(/at least one/i);
  });

  it('persists schema v1 atomically and appends without pruning prior lists', async () => {
    const storage = new MemoryStorage();
    const ids = ['manual-1', 'remote-2'];
    let now = 100;
    const store = new WatchlistStore(storage, {
      createId: () => ids.shift()!,
      now: () => now++,
    });

    expect(await store.hydrate()).toEqual([]);
    const manual = await store.saveManual('  Mega cap  ', ['aapl', 'MSFT', 'aapl']);
    const imported = await store.saveTradingView({
      name: ' Imported ',
      symbols: ['nvda', 'AAPL'],
      sourceUrl: 'https://www.tradingview.com/watchlists/123/',
      remoteId: '123',
    });

    expect(manual).toMatchObject({ id: 'manual-1', name: 'Mega cap', symbols: ['AAPL', 'MSFT'], source: { kind: 'manual' } });
    expect(imported).toMatchObject({ id: 'remote-2', name: 'Imported', symbols: ['NVDA', 'AAPL'], source: { kind: 'tradingview', remoteId: '123' } });
    expect(store.snapshot()).toHaveLength(2);
    expect(JSON.parse(storage.values.get(LISTS_STORAGE_KEY)!)).toMatchObject({ schemaVersion: 1, lists: [{ id: 'manual-1' }, { id: 'remote-2' }] });
  });

  it('renames, edits symbols, and deletes saved lists through one durable queue', async () => {
    const storage = new MemoryStorage();
    let now = 100;
    const store = new WatchlistStore(storage, { createId: () => 'list-1', now: () => now++ });
    await store.hydrate();
    const created = await store.saveManual('Mega cap', ['AAPL', 'MSFT']);

    const renamed = await store.rename(created.id, '  Core names  ');
    expect(renamed).toMatchObject({ name: 'Core names', symbols: ['AAPL', 'MSFT'] });
    expect(renamed.updatedAt).toBeGreaterThan(created.updatedAt);

    expect(await store.addSymbol(created.id, ' nvda ')).toMatchObject({ symbols: ['AAPL', 'MSFT', 'NVDA'] });
    await expect(store.addSymbol(created.id, 'nvda')).rejects.toThrow(/already in Core names/);
    await expect(store.addSymbol(created.id, 'bad/symbol')).rejects.toThrow(/1-32 letters/);
    expect(await store.removeSymbol(created.id, 'msft')).toMatchObject({ symbols: ['AAPL', 'NVDA'] });
    await expect(store.removeSymbol(created.id, 'TSLA')).rejects.toThrow(/not in Core names/);

    expect(JSON.parse(storage.values.get(LISTS_STORAGE_KEY)!)).toMatchObject({
      schemaVersion: 1,
      lists: [{ id: 'list-1', name: 'Core names', symbols: ['AAPL', 'NVDA'] }],
    });

    await store.removeSymbol(created.id, 'NVDA');
    await expect(store.removeSymbol(created.id, 'AAPL')).rejects.toThrow(/at least one symbol/);

    await store.remove(created.id);
    expect(store.snapshot()).toEqual([]);
    await expect(store.remove(created.id)).rejects.toThrow(/no longer saved/);
    await expect(store.rename('missing', 'Anything')).rejects.toThrow(/no longer saved/);
  });

  it('keeps the ten-symbol ceiling when adding to a full list', async () => {
    const store = new WatchlistStore(new MemoryStorage(), { createId: () => 'full', now: () => 1 });
    await store.hydrate();
    const full = await store.saveManual('Full', ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J']);

    await expect(store.addSymbol(full.id, 'K')).rejects.toThrow(/at most 10 symbols/);
    expect(store.snapshot()[0].symbols).toHaveLength(10);
  });

  it('does not mutate memory when durable persistence fails', async () => {
    const storage = new MemoryStorage();
    const store = new WatchlistStore(storage, { createId: () => 'one', now: () => 1 });
    await store.hydrate();
    storage.setItem = async () => {
      throw new Error('disk full');
    };

    await expect(store.saveManual('Keep safe', ['AAPL'])).rejects.toThrow('disk full');
    expect(store.snapshot()).toEqual([]);
  });

  it('recovers from corrupt or invalid schema storage', async () => {
    const storage = new MemoryStorage();
    storage.values.set(LISTS_STORAGE_KEY, '{bad');
    const store = new WatchlistStore(storage);
    expect(await store.hydrate()).toEqual([]);
    expect(storage.values.has(LISTS_STORAGE_KEY)).toBe(false);

    storage.values.set(LISTS_STORAGE_KEY, JSON.stringify({ schemaVersion: 2, lists: [] }));
    expect(await store.hydrate()).toEqual([]);
    expect(storage.values.has(LISTS_STORAGE_KEY)).toBe(false);
  });

  it('keeps valid saved lists while repairing corrupt siblings', async () => {
    const storage = new MemoryStorage();
    const valid = {
      id: 'valid',
      name: 'Still here',
      symbols: ['AAPL'],
      source: { kind: 'manual' as const },
      createdAt: 1,
      updatedAt: 2,
    };
    storage.values.set(
      LISTS_STORAGE_KEY,
      JSON.stringify({ schemaVersion: 1, lists: [valid, { id: 'corrupt' }] }),
    );

    const store = new WatchlistStore(storage);
    expect(await store.hydrate()).toEqual([valid]);
    expect(store.droppedCorruptListCount).toBe(1);
    expect(JSON.parse(storage.values.get(LISTS_STORAGE_KEY)!)).toEqual({
      schemaVersion: 1,
      lists: [valid],
    });
  });

  it('selects the newest updated list without mutating saved order', () => {
    const lists = [
      { id: 'old', name: 'Old', symbols: ['AAPL'], source: { kind: 'manual' as const }, createdAt: 1, updatedAt: 10 },
      { id: 'new', name: 'New', symbols: ['NVDA'], source: { kind: 'manual' as const }, createdAt: 2, updatedAt: 20 },
    ];
    expect(newestSavedList(lists)?.id).toBe('new');
    expect(lists.map((list) => list.id)).toEqual(['old', 'new']);
  });

  it.each([
    'http://www.tradingview.com/watchlists/123/',
    'https://eviltradingview.com/watchlists/123/',
    'https://user:pass@www.tradingview.com/watchlists/123/',
    'https://www.tradingview.com:443/watchlists/123/',
    'https://www.tradingview.com/watchlists/private/',
    'https://www.tradingview.com/watchlists/123',
    'not a url',
  ])('rejects an unsafe TradingView URL: %s', (url) => {
    expect(() => validateTradingViewUrl(url)).toThrow(/TradingView/i);
  });

  it('returns the canonical URL and remote id for an accepted TradingView URL', () => {
    expect(validateTradingViewUrl(' https://tradingview.com/watchlists/123/ ')).toEqual({
      sourceUrl: 'https://tradingview.com/watchlists/123/',
      remoteId: '123',
    });
  });
});
