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
