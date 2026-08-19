import { MOBILE_AGENT_TOOLS } from '@/src/api/agentTools';
import {
  LIBRARY_NAMESPACE,
  LibraryStore,
  projectCompletedResearch,
  type LibraryStorage,
  type ResearchCompletion,
} from '@/src/features/library/library-store';

class MemoryStorage implements LibraryStorage {
  readonly values = new Map<string, string>();

  async getItem(key: string) { return this.values.get(key) ?? null; }
  async setItem(key: string, value: string) { this.values.set(key, value); }
  async removeItem(key: string) { this.values.delete(key); }
  async getAllKeys() { return [...this.values.keys()]; }
  async multiRemove(keys: readonly string[]) { keys.forEach((key) => this.values.delete(key)); }
}

class GateStorage extends MemoryStorage {
  readonly events: string[] = [];
  writeStarted: (() => void) | null = null;
  releaseWrite: Promise<void> | null = null;
  failMultiRemove = false;

  override async setItem(key: string, value: string) {
    if (key.startsWith(LIBRARY_NAMESPACE) && this.releaseWrite) {
      this.events.push('touch:start');
      this.writeStarted?.();
      await this.releaseWrite;
      this.releaseWrite = null;
      this.events.push('touch:finish');
    }
    await super.setItem(key, value);
  }

  override async multiRemove(keys: readonly string[]) {
    this.events.push('multiRemove');
    if (this.failMultiRemove) throw new Error('prune failed');
    await super.multiRemove(keys);
  }
}

function completion(overrides: Partial<ResearchCompletion> = {}): ResearchCompletion {
  return {
    status: 'completed',
    symbol: 'AAPL',
    period: '1y',
    summary: 'Demand remains resilient with provider disagreement noted.',
    model: 'claude-sonnet',
    tools: [...MOBILE_AGENT_TOOLS],
    toolTrace: [{ name: 'analyze_ticker', status: 'completed', durationMs: 24, error: null }],
    artifacts: [{ type: 'chart', title: 'AAPL price', ticker: 'AAPL', content: 'discard me' }],
    generatedAt: 100,
    transport: 'stream',
    ...overrides,
  };
}

describe('LibraryStore', () => {
  it('strictly projects a completed result without request bodies, secret fields, or content artifacts', () => {
    const projected = projectCompletedResearch({
      ...completion(),
      requestBody: { authorization: 'Bearer private' },
      artifacts: [
        { type: 'chart', title: 'Safe metadata', ticker: 'AAPL', content: 'raw output' },
        { type: 'image', title: 'Unsafe binary', base64: 'AAAA' },
      ],
    }, { id: 'run-1', cachedAt: 200 });

    expect(projected).toMatchObject({
      schemaVersion: 1,
      id: 'run-1',
      status: 'completed',
      symbol: 'AAPL',
      period: '1y',
      generatedAt: 100,
      cachedAt: 200,
      accessedAt: 200,
      source: { kind: 'research-agent', transport: 'stream' },
    });
    expect(projected.artifacts).toEqual([{ type: 'chart', title: 'Safe metadata', ticker: 'AAPL' }]);
    expect(JSON.stringify(projected)).not.toMatch(/authorization|Bearer private|requestBody|raw output|AAAA/i);
  });

  it.each([
    { status: 'streaming' },
    { status: 'cancelled' },
    { tools: MOBILE_AGENT_TOOLS.slice(0, -1) },
    { summary: 'Authorization: Bearer abcdefghijklmnopqrstuvwxyz' },
  ])('refuses incomplete or unsafe completion %j', (override) => {
    expect(() => projectCompletedResearch({ ...completion(), ...override }, { id: 'run-x', cachedAt: 200 })).toThrow();
  });

  const credential = (...parts: string[]) => parts.join('');
  it.each([
    { label: 'password', summary: credential('pass', 'word=', 'correct-horse-battery-staple') },
    { label: 'GitHub', summary: credential('github', '_token=', 'gh', 'p_', 'abcdefghijklmnopqrstuvwxyz123456') },
    { label: 'Slack', summary: credential('slack=', 'xo', 'xb-', '1234567890-abcdefghijklmnop') },
    { label: 'AWS', summary: credential('aws=', 'AK', 'IA', 'IOSFODNN7EXAMPLE') },
    { label: 'Google', summary: credential('google=', 'AI', 'za', 'SyA123456789012345678901234567890') },
    { label: 'Stripe', summary: credential('stripe=', 'sk_', 'live_', 'abcdefghijklmnopqrstuvwxyz') },
    { label: 'service role', summary: credential('service', '_role=', 'ey', 'Jabcdefghijkl.abcdefghijkl.signature') },
  ])('rejects common $label credential material in selected persisted text', ({ summary }) => {
    expect(() => projectCompletedResearch(completion({ summary }), { id: 'run-secret', cachedAt: 200 })).toThrow(/unsafe/i);
  });

  it('stores one schema-versioned record per key and reopens it while offline', async () => {
    const storage = new MemoryStorage();
    const store = new LibraryStore(storage, { now: () => 200, createId: () => 'run-1' });
    await store.save(completion());

    expect([...storage.values.keys()]).toEqual([`${LIBRARY_NAMESPACE}run-1`]);
    const reopened = await store.read('run-1');
    expect(reopened).toMatchObject({ id: 'run-1', symbol: 'AAPL', summary: expect.stringContaining('Demand') });
    expect((await store.list()).records).toHaveLength(1);
  });

  it('prunes least-recently-used records by count and reports pruning', async () => {
    const storage = new MemoryStorage();
    let now = 1;
    let id = 0;
    const store = new LibraryStore(storage, {
      now: () => now,
      createId: () => `run-${++id}`,
      limits: { maxEntries: 2, maxEntryBytes: 128 * 1024, maxTotalBytes: 3 * 1024 * 1024 },
    });
    await store.save(completion({ generatedAt: now++ }));
    await store.save(completion({ symbol: 'MSFT', generatedAt: now++ }));
    await store.read('run-1');
    now += 10;
    const receipt = await store.save(completion({ symbol: 'NVDA', generatedAt: now++ }));

    expect(receipt.prunedCount).toBe(1);
    expect(await store.read('run-2')).toBeNull();
    expect((await store.list()).records.map((record) => record.id)).toEqual(expect.arrayContaining(['run-1', 'run-3']));
  });

  it('enforces the per-record and total byte caps before or during pruning', async () => {
    const storage = new MemoryStorage();
    let id = 0;
    const store = new LibraryStore(storage, {
      createId: () => `run-${++id}`,
      limits: { maxEntries: 24, maxEntryBytes: 900, maxTotalBytes: 1_200 },
    });
    await expect(store.save(completion({ summary: 'x'.repeat(2_000) }))).rejects.toThrow(/128 KiB|too large/i);
    expect(storage.values.size).toBe(0);

    await store.save(completion({ summary: 'a'.repeat(180), generatedAt: 1 }));
    const receipt = await store.save(completion({ summary: 'b'.repeat(180), generatedAt: 2 }));
    expect(receipt.prunedCount).toBeGreaterThanOrEqual(0);
    const bytes = [...storage.values.values()].reduce((total, raw) => total + new TextEncoder().encode(raw).length, 0);
    expect(bytes).toBeLessThanOrEqual(1_200);
  });

  it('rolls back the newly written record when pruning fails', async () => {
    const storage = new GateStorage();
    let id = 0;
    const store = new LibraryStore(storage, {
      createId: () => `run-${++id}`,
      limits: { maxEntries: 1, maxEntryBytes: 128 * 1024, maxTotalBytes: 3 * 1024 * 1024 },
    });
    await store.save(completion());
    storage.failMultiRemove = true;

    await expect(store.save(completion({ symbol: 'MSFT' }))).rejects.toThrow('prune failed');
    expect(storage.values.has(`${LIBRARY_NAMESPACE}run-2`)).toBe(false);
    expect(storage.values.has(`${LIBRARY_NAMESPACE}run-1`)).toBe(true);
  });

  it('serializes Clear All after an in-flight save so the record cannot reappear', async () => {
    const storage = new GateStorage();
    let release!: () => void;
    let started!: () => void;
    const startedPromise = new Promise<void>((resolve) => { started = resolve; });
    storage.writeStarted = started;
    storage.releaseWrite = new Promise<void>((resolve) => { release = resolve; });
    const store = new LibraryStore(storage, { createId: () => 'run-1' });

    const save = store.save(completion());
    await startedPromise;
    const clear = store.clear();
    release();
    await save;
    await clear;
    expect((await store.list()).records).toEqual([]);
  });

  it('serializes the read touch before Clear All so a stale touch cannot resurrect a record', async () => {
    const storage = new GateStorage();
    const store = new LibraryStore(storage, { createId: () => 'run-1' });
    await store.save(completion());
    storage.events.length = 0;
    let release!: () => void;
    let started!: () => void;
    const startedPromise = new Promise<void>((resolve) => { started = resolve; });
    storage.writeStarted = started;
    storage.releaseWrite = new Promise<void>((resolve) => { release = resolve; });

    const read = store.read('run-1');
    await startedPromise;
    const clear = store.clear();
    await Promise.resolve();
    await Promise.resolve();
    expect(storage.events).toEqual(['touch:start']);
    release();
    await read;
    await clear;
    expect((await store.list()).records).toEqual([]);
  });

  it('serializes corruption cleanup with a valid save that reuses the recovered id', async () => {
    const storage = new MemoryStorage();
    storage.values.set(`${LIBRARY_NAMESPACE}run-1`, '{bad json');
    const store = new LibraryStore(storage, { createId: () => 'run-1' });

    const cleanup = store.list();
    const save = store.save(completion());
    expect(await cleanup).toEqual({ records: [], corruptedCount: 1 });
    await save;
    expect(await store.read('run-1')).toMatchObject({ id: 'run-1', symbol: 'AAPL' });
  });

  it('removes corrupt records and supports delete plus clear all without touching other namespaces', async () => {
    const storage = new MemoryStorage();
    storage.values.set(`${LIBRARY_NAMESPACE}broken`, '{bad json');
    storage.values.set('@undercurrent/lists/v1', '{"keep":true}');
    let id = 0;
    const store = new LibraryStore(storage, { createId: () => `run-${++id}` });

    const corrupt = await store.list();
    expect(corrupt).toEqual({ records: [], corruptedCount: 1 });
    await store.save(completion());
    await store.save(completion({ symbol: 'MSFT' }));
    await store.delete('run-1');
    expect((await store.list()).records.map((record) => record.id)).toEqual(['run-2']);
    await store.clear();
    expect((await store.list()).records).toEqual([]);
    expect(storage.values.get('@undercurrent/lists/v1')).toBe('{"keep":true}');
  });
});
