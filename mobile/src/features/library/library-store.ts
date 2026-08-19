import AsyncStorage from '@react-native-async-storage/async-storage';

import { exactMobileToolEcho, MOBILE_AGENT_TOOLS } from '@/src/api/agentTools';
import { normalizeSymbol } from '@/src/api/endpoints';
import { normalizeResearchPeriod, type ResearchPeriod } from '@/src/features/research/research-model';
import { utf8Bytes } from '@/src/state/cache';

export const LIBRARY_NAMESPACE = '@undercurrent/library/v1/';
export const LIBRARY_SCHEMA_VERSION = 1;
export const LIBRARY_LIMITS = {
  maxEntries: 24,
  maxEntryBytes: 128 * 1024,
  maxTotalBytes: 3 * 1024 * 1024,
} as const;

export type ResearchTraceEntry = {
  name: string;
  status: 'started' | 'completed' | 'failed';
  durationMs: number | null;
  error: string | null;
};

export type ResearchArtifactInput = Record<string, unknown>;

export type ResearchCompletion = {
  status: 'completed';
  symbol: string;
  period: ResearchPeriod | string;
  summary: string;
  model: string;
  tools: readonly string[];
  toolTrace: readonly ResearchTraceEntry[];
  artifacts: readonly ResearchArtifactInput[];
  generatedAt: number;
  transport: 'stream' | 'fallback';
};

export type LibraryArtifact = {
  type?: string;
  title?: string;
  label?: string;
  ticker?: string;
  period?: string;
  source?: string;
  provider?: string;
};

export type LibraryRecord = {
  schemaVersion: typeof LIBRARY_SCHEMA_VERSION;
  id: string;
  status: 'completed';
  symbol: string;
  period: ResearchPeriod;
  summary: string;
  model: string;
  tools: string[];
  toolTrace: ResearchTraceEntry[];
  artifacts: LibraryArtifact[];
  source: { kind: 'research-agent'; transport: 'stream' | 'fallback' };
  generatedAt: number;
  cachedAt: number;
  accessedAt: number;
};

export type LibraryStorage = Pick<
  typeof AsyncStorage,
  'getItem' | 'setItem' | 'removeItem' | 'getAllKeys' | 'multiRemove'
>;

type LibraryLimits = {
  maxEntries: number;
  maxEntryBytes: number;
  maxTotalBytes: number;
};

type LibraryStoreOptions = {
  now?: () => number;
  createId?: () => string;
  limits?: LibraryLimits;
};

export type LibrarySnapshot = { records: LibraryRecord[]; corruptedCount: number };
export type LibrarySaveReceipt = { record: LibraryRecord; prunedCount: number };

const ALLOWED_ARTIFACT_KEYS = ['type', 'title', 'label', 'ticker', 'period', 'source', 'provider'] as const;
const DISALLOWED_ARTIFACT_KEYS = /^(?:data|base64|bytes|blob|binary)$/i;
const SECRET_TEXT = new RegExp(
  [
    'authorization\\s*:\\s*bearer\\s+\\S+',
    '(?:api[_-]?key|secret|password|token|github[_-]?token|slack|aws|google|stripe|service[_-]?role|access[_-]?token)\\s*[:=]\\s*\\S{8,}',
    '\\bgh[pousr]_[A-Za-z0-9]{20,}',
    '\\bxox[baprs]-[A-Za-z0-9-]{12,}',
    '\\b(?:AKIA|ASIA)[A-Z0-9]{16}',
    '\\bAIza[A-Za-z0-9_-]{30,}',
    '\\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}',
    '\\bsk-[A-Za-z0-9_-]{12,}',
    '-----BEGIN [A-Z ]*PRIVATE KEY-----',
    '\\beyJ[A-Za-z0-9_-]{12,}\\.[A-Za-z0-9_-]{12,}\\.',
  ].join('|'),
  'i',
);

function defaultId(): string {
  return `research-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 9)}`;
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

function safeString(value: unknown, label: string, options: { allowEmpty?: boolean } = {}): string {
  if (typeof value !== 'string' || (!options.allowEmpty && !value.trim())) {
    throw new Error(`${label} must be a string.`);
  }
  if (/^data:[^;]+;base64,/i.test(value) || SECRET_TEXT.test(value)) {
    throw new Error(`${label} contains unsafe content.`);
  }
  return value;
}

function safeId(value: string): string {
  if (!/^[A-Za-z0-9._-]{1,128}$/.test(value)) throw new Error('Library record id is invalid.');
  return value;
}

function projectTrace(value: unknown): ResearchTraceEntry[] {
  if (!Array.isArray(value)) throw new Error('Research tool trace must be an array.');
  return value.map((item) => {
    if (typeof item !== 'object' || item === null) throw new Error('Research tool trace entry is invalid.');
    const trace = item as Partial<ResearchTraceEntry>;
    const name = safeString(trace.name, 'Research tool name');
    if (!(MOBILE_AGENT_TOOLS as readonly string[]).includes(name)) {
      throw new Error(`Research tool ${name} is outside the mobile allowlist.`);
    }
    if (!['started', 'completed', 'failed'].includes(String(trace.status))) {
      throw new Error('Research tool status is invalid.');
    }
    const durationMs = trace.durationMs === null || trace.durationMs === undefined
      ? null
      : isFiniteNumber(trace.durationMs) && trace.durationMs >= 0
        ? trace.durationMs
        : null;
    const error = trace.error === null || trace.error === undefined
      ? null
      : safeString(trace.error, 'Research tool error', { allowEmpty: true });
    return { name, status: trace.status as ResearchTraceEntry['status'], durationMs, error };
  });
}

function projectArtifacts(value: unknown): LibraryArtifact[] {
  if (!Array.isArray(value)) throw new Error('Research artifacts must be an array.');
  return value.flatMap((item) => {
    if (typeof item !== 'object' || item === null || Array.isArray(item)) return [];
    const artifact = item as Record<string, unknown>;
    if (
      Object.entries(artifact).some(
        ([key, child]) => DISALLOWED_ARTIFACT_KEYS.test(key) || (typeof child === 'string' && /^data:[^;]+;base64,/i.test(child)),
      )
    ) {
      return [];
    }
    const projected: LibraryArtifact = {};
    ALLOWED_ARTIFACT_KEYS.forEach((key) => {
      const child = artifact[key];
      if (typeof child === 'string' && child.trim() && !SECRET_TEXT.test(child)) projected[key] = child.slice(0, 500);
    });
    return Object.keys(projected).length ? [projected] : [];
  });
}

export function projectCompletedResearch(
  value: unknown,
  options: { id: string; cachedAt: number },
): LibraryRecord {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error('Research completion is invalid.');
  }
  const completion = value as Partial<ResearchCompletion> & { status?: unknown };
  if (completion.status !== 'completed') throw new Error('Only terminal completed research can be saved.');
  const tools = exactMobileToolEcho(completion.tools);
  if (!tools) throw new Error('Research completion has an invalid tool allowlist.');
  const generatedAt = completion.generatedAt;
  if (!isFiniteNumber(generatedAt)) throw new Error('Research generated time is invalid.');
  if (!isFiniteNumber(options.cachedAt)) throw new Error('Research cache time is invalid.');
  if (completion.transport !== 'stream' && completion.transport !== 'fallback') {
    throw new Error('Research transport is invalid.');
  }

  return {
    schemaVersion: LIBRARY_SCHEMA_VERSION,
    id: safeId(options.id),
    status: 'completed',
    symbol: normalizeSymbol(safeString(completion.symbol, 'Research symbol')),
    period: normalizeResearchPeriod(safeString(completion.period, 'Research period')),
    summary: safeString(completion.summary, 'Research summary', { allowEmpty: true }),
    model: safeString(completion.model, 'Research model'),
    tools,
    toolTrace: projectTrace(completion.toolTrace),
    artifacts: projectArtifacts(completion.artifacts),
    source: { kind: 'research-agent', transport: completion.transport },
    generatedAt,
    cachedAt: options.cachedAt,
    accessedAt: options.cachedAt,
  };
}

function parseRecord(raw: string): LibraryRecord | null {
  try {
    const value: unknown = JSON.parse(raw);
    if (typeof value !== 'object' || value === null || Array.isArray(value)) return null;
    const record = value as Partial<LibraryRecord>;
    if (
      record.schemaVersion !== LIBRARY_SCHEMA_VERSION
      || record.status !== 'completed'
      || !isFiniteNumber(record.cachedAt)
      || !isFiniteNumber(record.accessedAt)
      || !isFiniteNumber(record.generatedAt)
      || typeof record.source !== 'object'
      || record.source === null
      || record.source.kind !== 'research-agent'
    ) {
      return null;
    }
    const projected = projectCompletedResearch(
      {
        ...record,
        transport: record.source.transport,
      },
      { id: String(record.id), cachedAt: record.cachedAt },
    );
    return { ...projected, accessedAt: record.accessedAt };
  } catch {
    return null;
  }
}

export class LibraryStore {
  private readonly now: () => number;
  private readonly createId: () => string;
  private readonly limits: LibraryLimits;
  private writeQueue: Promise<void> = Promise.resolve();
  private lastAccessAt = 0;

  constructor(
    private readonly storage: LibraryStorage = AsyncStorage,
    options: LibraryStoreOptions = {},
  ) {
    this.now = options.now ?? Date.now;
    this.createId = options.createId ?? defaultId;
    this.limits = options.limits ?? LIBRARY_LIMITS;
  }

  list(): Promise<LibrarySnapshot> {
    return this.enqueue(async () => {
      const keys = (await this.storage.getAllKeys()).filter((key) => key.startsWith(LIBRARY_NAMESPACE));
      const records: LibraryRecord[] = [];
      const corrupt: string[] = [];
      for (const key of keys) {
        const raw = await this.storage.getItem(key);
        const record = raw === null ? null : parseRecord(raw);
        if (!record || key !== this.keyFor(record.id)) corrupt.push(key);
        else records.push(record);
      }
      if (corrupt.length) await this.storage.multiRemove(corrupt);
      records.sort((left, right) => right.generatedAt - left.generatedAt || right.cachedAt - left.cachedAt);
      return { records, corruptedCount: corrupt.length };
    });
  }

  read(id: string): Promise<LibraryRecord | null> {
    return this.enqueue(async () => {
      const key = this.keyFor(id);
      const raw = await this.storage.getItem(key);
      if (raw === null) return null;
      const record = parseRecord(raw);
      if (!record || record.id !== id) {
        await this.storage.removeItem(key);
        return null;
      }
      const touched = { ...record, accessedAt: this.timestamp() };
      await this.storage.setItem(key, JSON.stringify(touched));
      return touched;
    });
  }

  save(completion: ResearchCompletion): Promise<LibrarySaveReceipt> {
    return this.enqueue(async () => {
      const cachedAt = this.timestamp();
      const record = projectCompletedResearch(completion, { id: this.createId(), cachedAt });
      const serialized = JSON.stringify(record);
      if (utf8Bytes(serialized) > this.limits.maxEntryBytes) {
        throw new Error('Research record is too large for the 128 KiB Library limit.');
      }
      const key = this.keyFor(record.id);
      const existing = await this.storage.getItem(key);
      if (existing !== null) {
        if (parseRecord(existing)) throw new Error('Library record id already exists.');
        await this.storage.removeItem(key);
      }
      await this.storage.setItem(key, serialized);
      try {
        const prunedCount = await this.prune();
        return { record, prunedCount };
      } catch (error) {
        await this.storage.removeItem(key).catch(() => undefined);
        throw error;
      }
    });
  }

  delete(id: string): Promise<void> {
    return this.enqueue(() => this.storage.removeItem(this.keyFor(id)));
  }

  clear(): Promise<void> {
    return this.enqueue(async () => {
      const keys = (await this.storage.getAllKeys()).filter((key) => key.startsWith(LIBRARY_NAMESPACE));
      if (keys.length) await this.storage.multiRemove(keys);
    });
  }

  private keyFor(id: string): string {
    return `${LIBRARY_NAMESPACE}${safeId(id)}`;
  }

  private timestamp(): number {
    this.lastAccessAt = Math.max(this.now(), this.lastAccessAt + 1);
    return this.lastAccessAt;
  }

  private enqueue<T>(operation: () => Promise<T>): Promise<T> {
    const result = this.writeQueue.then(operation);
    this.writeQueue = result.then(() => undefined, () => undefined);
    return result;
  }

  private async prune(): Promise<number> {
    const keys = (await this.storage.getAllKeys()).filter((key) => key.startsWith(LIBRARY_NAMESPACE));
    const entries: { key: string; record: LibraryRecord; bytes: number }[] = [];
    const remove: string[] = [];
    for (const key of keys) {
      const raw = await this.storage.getItem(key);
      const record = raw === null ? null : parseRecord(raw);
      if (!raw || !record || key !== this.keyFor(record.id)) remove.push(key);
      else entries.push({ key, record, bytes: utf8Bytes(raw) });
    }
    entries.sort((left, right) => right.record.accessedAt - left.record.accessedAt || right.record.cachedAt - left.record.cachedAt);
    let kept = 0;
    let totalBytes = 0;
    entries.forEach((entry) => {
      if (kept >= this.limits.maxEntries || totalBytes + entry.bytes > this.limits.maxTotalBytes) {
        remove.push(entry.key);
        return;
      }
      kept += 1;
      totalBytes += entry.bytes;
    });
    if (remove.length) await this.storage.multiRemove(remove);
    return remove.length;
  }
}

export const defaultLibraryStore = new LibraryStore();
