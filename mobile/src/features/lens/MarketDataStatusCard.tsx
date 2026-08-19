import { useEffect, useState } from 'react';
import { StyleSheet, Text, View } from 'react-native';

import type { ApiClient } from '@/src/api/client';
import type { ProviderStatusResponse } from '@/src/api/contracts';
import AsyncState from '@/src/components/ui/AsyncState';
import { colors, radii, spacing, typography } from '@/src/theme/tokens';

type MarketDataStatusCardProps = { client: Pick<ApiClient, 'providers'> };

export default function MarketDataStatusCard({ client }: MarketDataStatusCardProps) {
  const [state, setState] = useState<{ status: 'loading' | 'ready' | 'error'; data?: ProviderStatusResponse; message?: string }>({ status: 'loading' });

  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    setState({ status: 'loading' });
    client.providers({ signal: controller.signal }).then((data) => {
      if (active) setState({ status: 'ready', data });
    }).catch((error: unknown) => {
      if (active && !controller.signal.aborted) {
        setState({ status: 'error', message: error instanceof Error ? error.message : 'Provider status unavailable.' });
      }
    });
    return () => {
      active = false;
      controller.abort();
    };
  }, [client]);

  if (state.status === 'loading') {
    return <AsyncState title="Checking market data" message="Confirming provider freshness and streaming access." />;
  }
  if (state.status === 'error' || !state.data) {
    return <AsyncState title="Market data status unavailable" message={state.message ?? 'Provider status could not be read.'} tone="warning" />;
  }

  const { data } = state;
  const streamReady = data.streaming.enabled && data.streaming.configured;
  return (
    <View accessibilityLabel="Market data status" style={styles.card}>
      <View style={styles.heading}>
        <Text style={styles.eyebrow}>MARKET DATA STATUS</Text>
        <Text style={styles.primary}>{data.primary.toUpperCase()} PRIMARY</Text>
      </View>
      <View style={styles.grid}>
        <StatusMetric label="STOCKS" value={data.freshness.stocks} />
        <StatusMetric label="OPTIONS" value={data.freshness.options} />
        <StatusMetric label="STREAM" value={streamReady ? data.streaming.freshness : 'Unavailable'} />
      </View>
      <Text style={styles.note}>
        {streamReady ? 'Realtime stock and options events are available.' : 'Streaming is not currently available for this session.'}
      </Text>
    </View>
  );
}

function StatusMetric({ label, value }: { label: string; value: string }) {
  return (
    <View style={styles.metric}>
      <Text style={styles.metricLabel}>{label}</Text>
      <Text style={styles.metricValue}>{value}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  card: {
    backgroundColor: colors.graphiteRaised,
    borderColor: colors.mineral,
    borderRadius: radii.lg,
    borderWidth: 1,
    gap: spacing.sm,
    padding: spacing.md,
  },
  heading: { alignItems: 'center', flexDirection: 'row', justifyContent: 'space-between' },
  eyebrow: { ...typography.eyebrow, color: colors.inkMuted },
  primary: { ...typography.caption, color: colors.mint },
  grid: { flexDirection: 'row', gap: spacing.sm },
  metric: { flex: 1, gap: spacing.xs },
  metricLabel: { ...typography.eyebrow, color: colors.inkMuted },
  metricValue: { ...typography.caption, color: colors.ink },
  note: { ...typography.caption, color: colors.inkSecondary },
});
