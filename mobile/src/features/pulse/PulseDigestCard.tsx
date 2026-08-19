import { StyleSheet, Text, View } from 'react-native';

import type { AlertDigest } from '@/src/api/contracts';
import { colors, radii, spacing, typography } from '@/src/theme/tokens';

type PulseDigestCardProps = {
  digest: AlertDigest;
  freshness: string;
  sourceLabel: string;
};

function tickerLine(label: string, tickers: readonly string[]): string | null {
  return tickers.length ? `${label} · ${tickers.join(' · ')}` : null;
}

export default function PulseDigestCard({ digest, freshness, sourceLabel }: PulseDigestCardProps) {
  const signals = [
    tickerLine('Priority', digest.priorityTickers),
    tickerLine('Risk', digest.riskTickers),
    tickerLine('Flow shift', digest.flowShiftTickers),
  ].filter((value): value is string => Boolean(value));

  return (
    <View accessibilityLabel="Market pulse briefing" style={styles.card}>
      <View style={styles.topline}>
        <Text style={styles.eyebrow}>TODAY’S BRIEFING</Text>
        <Text style={styles.context}>{sourceLabel} · {freshness}</Text>
      </View>
      <Text accessibilityRole="header" style={styles.headline}>{digest.headline || 'Market pulse'}</Text>
      {digest.summary ? <Text style={styles.summary}>{digest.summary}</Text> : null}
      {signals.length ? (
        <View style={styles.signalStack}>
          {signals.map((signal) => <Text key={signal} style={styles.signal}>{signal}</Text>)}
        </View>
      ) : null}
      {digest.nextSteps.length ? (
        <View style={styles.nextStep}>
          <Text style={styles.nextLabel}>NEXT</Text>
          <Text style={styles.nextCopy}>{digest.nextSteps[0]}</Text>
        </View>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  card: {
    backgroundColor: colors.graphiteRaised,
    borderColor: colors.cyan,
    borderRadius: radii.lg,
    borderWidth: 1,
    gap: spacing.sm,
    padding: spacing.lg,
  },
  topline: { flexDirection: 'row', flexWrap: 'wrap', gap: spacing.sm, justifyContent: 'space-between' },
  eyebrow: { ...typography.eyebrow, color: colors.cyan },
  context: { ...typography.micro, color: colors.inkMuted },
  headline: { ...typography.headline, color: colors.ink },
  summary: { ...typography.body, color: colors.inkSecondary },
  signalStack: { flexDirection: 'row', flexWrap: 'wrap', gap: spacing.xs },
  signal: {
    ...typography.micro,
    backgroundColor: colors.graphiteSoft,
    borderRadius: radii.pill,
    color: colors.inkSecondary,
    overflow: 'hidden',
    paddingHorizontal: spacing.sm,
    paddingVertical: spacing.xs,
  },
  nextStep: {
    alignItems: 'flex-start',
    borderTopColor: colors.mineral,
    borderTopWidth: StyleSheet.hairlineWidth,
    flexDirection: 'row',
    gap: spacing.sm,
    paddingTop: spacing.sm,
  },
  nextLabel: { ...typography.micro, color: colors.mint },
  nextCopy: { ...typography.body, color: colors.ink, flex: 1 },
});
