export function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

export function explicitProvider(...values: unknown[]): string {
  for (const value of values) {
    if (typeof value === 'string' && value.trim()) return value.trim();
  }
  return 'Source not reported';
}

export function formatTimestamp(value: number): string {
  return `Updated ${new Date(value).toLocaleString()}`;
}

const MINUTE_MS = 60_000;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;

/**
 * A short freshness stamp for chart headers, where the full locale timestamp
 * wraps onto two lines and buries the reading below it. Absolute dates still win
 * once "ago" stops being meaningful.
 */
export function formatFreshness(value: number, now: () => number = Date.now): string {
  const elapsed = now() - value;
  if (!Number.isFinite(elapsed) || elapsed < 0) return formatTimestamp(value);
  if (elapsed < MINUTE_MS) return 'Updated just now';
  if (elapsed < HOUR_MS) return `Updated ${Math.floor(elapsed / MINUTE_MS)}m ago`;
  if (elapsed < DAY_MS) return `Updated ${Math.floor(elapsed / HOUR_MS)}h ago`;
  return `Updated ${new Date(value).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}`;
}

// Provider notes double as internal pipeline labels ("Batch auction chart data").
// Those say nothing to a reader, so only genuine provider caveats reach the surface.
const INTERNAL_NOTE = /^(?:batch|mixed provider)\b.*\b(?:chart data|render|brief)$/i;

export function publicProviderNote(note: string | null | undefined): string | null {
  const trimmed = typeof note === 'string' ? note.trim() : '';
  if (!trimmed || INTERNAL_NOTE.test(trimmed)) return null;
  return trimmed;
}
