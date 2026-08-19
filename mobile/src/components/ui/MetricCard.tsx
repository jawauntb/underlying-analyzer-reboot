import type { ReactNode } from 'react';
import { StyleSheet, Text, View } from 'react-native';

import { colors, radii, spacing, typography } from '@/src/theme/tokens';

type MetricCardProps = {
  label: string;
  value: string;
  detail?: string;
  accent?: 'mint' | 'cyan' | 'coral';
  children?: ReactNode;
};

export default function MetricCard({ label, value, detail, accent = 'cyan', children }: MetricCardProps) {
  return (
    <View style={[styles.card, { borderTopColor: colors[accent] }]}>
      <Text style={styles.label}>{label}</Text>
      <Text style={styles.value}>{value}</Text>
      {detail ? <Text style={styles.detail}>{detail}</Text> : null}
      {children}
    </View>
  );
}

const styles = StyleSheet.create({
  card: {
    backgroundColor: colors.graphiteRaised,
    borderColor: colors.mineral,
    borderRadius: radii.lg,
    borderTopWidth: 3,
    borderWidth: 1,
    gap: spacing.xs,
    padding: spacing.md,
    width: '100%',
  },
  label: { ...typography.eyebrow, color: colors.inkMuted },
  value: { ...typography.title, color: colors.ink },
  detail: { ...typography.caption, color: colors.inkSecondary },
});
