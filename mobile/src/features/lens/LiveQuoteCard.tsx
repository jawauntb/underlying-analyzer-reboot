import { useEffect, useState } from 'react';
import { StyleSheet, Text, View } from 'react-native';

import type { ApiClient } from '@/src/api/client';
import type { MarketSnapshotResponse } from '@/src/api/contracts';
import AsyncState from '@/src/components/ui/AsyncState';
import { colors, radii, spacing, typography } from '@/src/theme/tokens';

type LiveQuoteCardProps = { client: Pick<ApiClient, 'marketSnapshot'>; symbol: string };

function record(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function number(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function quoteValue(data: Record<string, unknown>): number | null {
  const lastTrade = record(data.lastTrade ?? data.last_trade);
  const day = record(data.day);
  return number(lastTrade.p) ?? number(lastTrade.price) ?? number(day.c) ?? number(day.close) ?? number(data.price);
}

function formatPrice(value: number | null): string {
  return value === null ? '—' : `$${value.toFixed(2)}`;
}

export default function LiveQuoteCard({ client, symbol }: LiveQuoteCardProps) {
  const [state, setState] = useState<{ status: 'loading' | 'ready' | 'error'; data?: MarketSnapshotResponse; message?: string }>({ status: 'loading' });

  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    setState({ status: 'loading' });
    client.marketSnapshot(symbol, { signal: controller.signal }).then((data) => {
      if (active) setState({ status: 'ready', data });
    }).catch((error: unknown) => {
      if (active && !controller.signal.aborted) {
        setState({ status: 'error', message: error instanceof Error ? error.message : 'Live quote unavailable.' });
      }
    });
    return () => {
      active = false;
      controller.abort();
    };
  }, [client, symbol]);

  if (state.status === 'loading') {
    return <AsyncState title="Loading live quote" message="Reading the latest Massive market snapshot." />;
  }
  if (state.status === 'error' || !state.data) {
    return <AsyncState title="Live quote unavailable" message={state.message ?? 'No quote snapshot was returned.'} tone="warning" />;
  }

  const price = quoteValue(state.data.data);
  return (
    <View accessibilityLabel={`${symbol} live quote`} style={styles.card}>
      <View style={styles.heading}>
        <Text style={styles.eyebrow}>MASSIVE LIVE SNAPSHOT</Text>
        <Text style={styles.provider}>{state.data.provider}</Text>
      </View>
      <Text style={styles.price}>{formatPrice(price)}</Text>
      <Text style={styles.note}>{state.data.providerNote ?? 'Latest available provider snapshot.'}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  card: {
    backgroundColor: colors.graphiteRaised,
    borderColor: colors.cyan,
    borderRadius: radii.lg,
    borderWidth: 1,
    gap: spacing.xs,
    padding: spacing.md,
  },
  heading: { alignItems: 'center', flexDirection: 'row', justifyContent: 'space-between' },
  eyebrow: { ...typography.eyebrow, color: colors.cyan },
  provider: { ...typography.caption, color: colors.inkMuted },
  price: { ...typography.display, color: colors.mint },
  note: { ...typography.caption, color: colors.inkSecondary },
});
