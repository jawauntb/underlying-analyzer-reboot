import { ScrollView, StyleSheet, Text, View } from 'react-native';

import type { OptionChainRow, OptionsChainResponse } from '@/src/api/contracts';
import { colors, radii, spacing, typography } from '@/src/theme/tokens';

type OptionsPulseCardProps = { data: OptionsChainResponse; symbol: string };

function formatPrice(value: number | null): string {
  return value === null || value === 0 ? '—' : `$${value.toFixed(2)}`;
}

function formatPercent(value: number | null): string {
  return value === null ? '—' : `${(value * 100).toFixed(1)}%`;
}

function formatDelta(value: number | null): string {
  return value === null ? '—' : value.toFixed(2);
}

function formatPair(left: number | null, right: number | null): string {
  return `${formatPrice(left)} / ${formatPrice(right)}`;
}

export default function OptionsPulseCard({ data, symbol }: OptionsPulseCardProps) {
  return (
    <View accessibilityLabel={`${symbol} options pulse`} style={styles.card}>
      <View style={styles.heading}>
        <View>
          <Text style={styles.eyebrow}>OPTIONS PULSE</Text>
          <Text style={styles.title}>{data.expiry} · {formatPrice(data.currentPrice)}</Text>
        </View>
        <Text style={styles.provider}>{data.provider}</Text>
      </View>
      <Text style={styles.note}>{data.providerNote ?? 'Nearest expiry with IV, Greeks, quotes, volume, and open interest.'}</Text>
      <ScrollView horizontal showsHorizontalScrollIndicator={false}>
        <View style={styles.table}>
          <View style={styles.row}>
            <Text style={[styles.cell, styles.headerCell]}>STRIKE</Text>
            <Text style={[styles.cell, styles.headerCell]}>CALL IV</Text>
            <Text style={[styles.cell, styles.headerCell]}>PUT IV</Text>
            <Text style={[styles.cell, styles.headerCell]}>CALL Δ</Text>
            <Text style={[styles.cell, styles.headerCell]}>PUT Δ</Text>
            <Text style={[styles.cell, styles.headerCell]}>CALL BID/ASK</Text>
            <Text style={[styles.cell, styles.headerCell]}>PUT BID/ASK</Text>
            <Text style={[styles.cell, styles.headerCell]}>OI C/P</Text>
          </View>
          {data.rows.map((row) => <OptionsRow key={row.strike} row={row} />)}
        </View>
      </ScrollView>
    </View>
  );
}

function OptionsRow({ row }: { row: OptionChainRow }) {
  return (
    <View style={styles.row}>
      <Text style={[styles.cell, styles.strike]}>{row.strike.toFixed(1)}</Text>
      <Text style={styles.cell}>{formatPercent(row.callImpliedVolatility)}</Text>
      <Text style={styles.cell}>{formatPercent(row.putImpliedVolatility)}</Text>
      <Text style={styles.cell}>{formatDelta(row.callDelta)}</Text>
      <Text style={styles.cell}>{formatDelta(row.putDelta)}</Text>
      <Text style={styles.cell}>{formatPair(row.callBid, row.callAsk)}</Text>
      <Text style={styles.cell}>{formatPair(row.putBid, row.putAsk)}</Text>
      <Text style={styles.cell}>{row.callOpenInterest.toFixed(0)} / {row.putOpenInterest.toFixed(0)}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  card: {
    backgroundColor: colors.graphiteRaised,
    borderColor: colors.coral,
    borderRadius: radii.lg,
    borderWidth: 1,
    gap: spacing.sm,
    padding: spacing.md,
  },
  heading: { alignItems: 'center', flexDirection: 'row', justifyContent: 'space-between' },
  eyebrow: { ...typography.eyebrow, color: colors.coral },
  title: { ...typography.title, color: colors.ink, marginTop: spacing.xs },
  provider: { ...typography.caption, color: colors.inkMuted },
  note: { ...typography.caption, color: colors.inkSecondary },
  table: { minWidth: 760 },
  row: { alignItems: 'center', borderBottomColor: colors.mineral, borderBottomWidth: 1, flexDirection: 'row', minHeight: 36 },
  cell: { ...typography.caption, color: colors.inkSecondary, paddingHorizontal: spacing.xs, width: 92 },
  headerCell: { color: colors.inkMuted, fontSize: 10 },
  strike: { color: colors.ink, fontWeight: '700' },
});
