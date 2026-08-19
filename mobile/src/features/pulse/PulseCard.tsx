import Ionicons from '@expo/vector-icons/Ionicons';
import { Pressable, StyleSheet, Text, View } from 'react-native';

import type { AlertRow } from '@/src/api/contracts';
import { colors, layout, radii, spacing, typography } from '@/src/theme/tokens';

type PulseCardProps = {
  row: AlertRow;
  fallbackRank: number;
  provider: string | null;
  freshness: string;
  onPress: () => void;
};

function number(value: number | null | undefined, suffix = ''): string {
  return typeof value === 'number' && Number.isFinite(value) ? `${value.toFixed(1)}${suffix}` : 'Unavailable';
}

export default function PulseCard({ row, fallbackRank, provider, freshness, onPress }: PulseCardProps) {
  const rank = row.rank ?? fallbackRank;
  const provenance = row.provider ?? provider ?? 'Provider unavailable';
  const price = typeof row.price === 'number' ? `$${row.price.toFixed(2)}` : null;
  const change = typeof row.changePercent === 'number' ? `${row.changePercent >= 0 ? '+' : ''}${row.changePercent.toFixed(2)}%` : null;

  return (
    <Pressable
      accessibilityHint="Opens the shared ticker Lens"
      accessibilityLabel={`Open ${row.ticker} Lens`}
      accessibilityRole="button"
      onPress={onPress}
      style={({ pressed }) => [styles.card, pressed && styles.pressed]}>
      <View style={styles.topline}>
        <View style={styles.rankWell}>
          <Text style={styles.rankLabel}>RANK</Text>
          <Text style={styles.rank}>#{rank}</Text>
        </View>
        <View style={styles.identity}>
          <Text style={styles.ticker}>{row.ticker}</Text>
          <Text style={styles.lane}>{row.lane ?? 'Review lane'}</Text>
        </View>
        <Ionicons color={colors.inkMuted} name="arrow-forward" size={20} />
      </View>

      <View style={styles.metrics}>
        <View style={styles.metric}>
          <Text style={styles.metricLabel}>SCORE</Text>
          <Text style={styles.metricValue}>{number(row.score)}</Text>
        </View>
        <View style={styles.metric}>
          <Text style={styles.metricLabel}>SCANNER</Text>
          <Text style={styles.metricValue}>{number(row.scannerScore)}</Text>
        </View>
        {price || change ? (
          <View style={styles.metric}>
            <Text style={styles.metricLabel}>MARKET</Text>
            <Text style={styles.metricValue}>{[price, change].filter(Boolean).join(' · ')}</Text>
          </View>
        ) : null}
      </View>

      <View style={styles.detail}>
        <Text style={styles.setupLabel}>SETUP</Text>
        <Text style={styles.setup}>{row.setup ?? 'No setup description available.'}</Text>
      </View>
      <Text style={styles.provenance}>{provenance} · {freshness}</Text>
      {row.providerNote ? <Text style={styles.providerNote}>{row.providerNote}</Text> : null}
    </Pressable>
  );
}

const styles = StyleSheet.create({
  card: {
    backgroundColor: colors.graphiteRaised,
    borderColor: colors.mineral,
    borderRadius: radii.xl,
    borderWidth: 1,
    gap: spacing.md,
    minHeight: layout.minimumTouchTarget,
    padding: spacing.md,
  },
  pressed: { opacity: 0.72 },
  topline: { alignItems: 'center', flexDirection: 'row', gap: spacing.sm },
  rankWell: {
    alignItems: 'center',
    backgroundColor: colors.mineralSoft,
    borderRadius: radii.md,
    minWidth: 56,
    padding: spacing.sm,
  },
  rankLabel: { ...typography.micro, color: colors.inkMuted },
  rank: { ...typography.label, color: colors.cyan },
  identity: { flex: 1, gap: 2 },
  ticker: { ...typography.headline, color: colors.ink },
  lane: { ...typography.caption, color: colors.inkSecondary },
  metrics: { flexDirection: 'row', flexWrap: 'wrap', gap: spacing.sm },
  metric: { flexGrow: 1, gap: 2, minWidth: 88 },
  metricLabel: { ...typography.micro, color: colors.inkMuted },
  metricValue: { ...typography.label, color: colors.ink },
  detail: { borderTopColor: colors.mineral, borderTopWidth: 1, gap: spacing.xs, paddingTop: spacing.sm },
  setupLabel: { ...typography.micro, color: colors.coral },
  setup: { ...typography.body, color: colors.inkSecondary },
  provenance: { ...typography.caption, color: colors.mint },
  providerNote: { ...typography.caption, color: colors.inkMuted },
});
