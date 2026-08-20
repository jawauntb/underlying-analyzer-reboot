import { StyleSheet, Text, useWindowDimensions, View } from 'react-native';

import { chartColors, colors, radii, spacing, typography } from '@/src/theme/tokens';

import type { ChartDataRow } from './ChartDataTable';
import { LineChart, type ChartLine } from './LineChart';
import { normalizeTorqueChart, type FundamentalPoint } from './models';

type TorqueChartProps = {
  dataset: unknown;
  fontScale?: number;
  title?: string;
  width?: number;
};

function fundamentalRows(label: string, key: string, points: readonly FundamentalPoint[]): ChartDataRow[] {
  return points.map((point, index) => ({
    key: `torque-${key}-${index}-${point.label}`,
    label: point.label,
    cells: [{ label, value: String(point.value) }],
  }));
}

export function TorqueChart({
  dataset,
  fontScale: requestedFontScale,
  title = 'Torque',
  width: requestedWidth,
}: TorqueChartProps) {
  const window = useWindowDimensions();
  const width = requestedWidth ?? window.width;
  const fontScale = requestedFontScale ?? window.fontScale;
  const compact = width < 350 || fontScale >= 1.3;
  const model = normalizeTorqueChart(dataset);
  const lines: ChartLine[] = [
    { key: 'close', label: 'Close', color: chartColors.primary, mark: 'line', points: model.priceLines.close, spoken: 'solid line', width: 2.5 },
    { key: 'ema75', label: 'EMA 75', color: chartColors.secondary, dashArray: '7 4', mark: 'dashed', points: model.priceLines.ema75, spoken: 'dashed line' },
    { key: 'sma50', label: 'SMA 50', color: chartColors.positive, dashArray: '9 3 2 3', mark: 'dash-dot', points: model.priceLines.sma50, spoken: 'dash-dot line' },
    { key: 'sma200', label: 'SMA 200', color: chartColors.negative, dashArray: '2 4', mark: 'dotted', points: model.priceLines.sma200, spoken: 'dotted line' },
  ];
  const technicalRows: ChartDataRow[] = model.categories.map((date, categoryIndex) => ({
    key: `torque-price-${categoryIndex}-${date}`,
    label: date,
    cells: lines.flatMap((line) => {
      const point = line.points.find((candidate) => candidate.categoryIndex === categoryIndex);
      return point ? [{ label: line.label, value: String(point.value) }] : [];
    }),
  }));
  const tableRows = [
    ...technicalRows,
    ...fundamentalRows('Revenue', 'revenue', model.fundamentals.revenue),
    ...fundamentalRows('Gross margin', 'gross-margin', model.fundamentals.grossMargin),
    ...fundamentalRows('Operating margin', 'operating-margin', model.fundamentals.operatingMargin),
  ];

  return (
    <View style={styles.surface}>
      <LineChart
        fontScale={fontScale}
        lines={lines}
        tableRows={tableRows}
        title={title}
        unavailableMessage="Technical price data unavailable."
        warnings={model.warnings.filter((warning) => !warning.startsWith('Fundamental data unavailable'))}
        width={width}
      />

      {model.technicalOnly ? (
        <Text accessibilityRole="alert" style={styles.technicalOnly}>
          Fundamental data unavailable — technicals only.
        </Text>
      ) : (
        <View testID={`${title}-fundamentals`} style={[styles.fundamentals, compact && styles.fundamentalsCompact]}>
          <FundamentalSummary label="Revenue" points={model.fundamentals.revenue} />
          <FundamentalSummary label="Gross margin" points={model.fundamentals.grossMargin} />
          <FundamentalSummary label="Operating margin" points={model.fundamentals.operatingMargin} />
        </View>
      )}
    </View>
  );
}

function FundamentalSummary({ label, points }: { label: string; points: readonly FundamentalPoint[] }) {
  const latest = points.at(-1);
  return (
    <View style={styles.fundamentalChip}>
      <Text style={styles.fundamentalLabel}>{label}</Text>
      <Text style={styles.fundamentalValue}>{latest ? `${latest.label} · ${latest.value}` : 'Unavailable'}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  surface: { gap: spacing.sm, width: '100%' },
  technicalOnly: {
    ...typography.caption,
    backgroundColor: colors.mineralSoft,
    borderRadius: radii.md,
    color: colors.coral,
    padding: spacing.sm,
  },
  fundamentals: { flexDirection: 'row', flexWrap: 'wrap', gap: spacing.sm },
  fundamentalsCompact: { flexDirection: 'column' },
  fundamentalChip: {
    backgroundColor: colors.graphiteRaised,
    borderColor: colors.mineral,
    borderRadius: radii.md,
    borderWidth: 1,
    flexGrow: 1,
    gap: 2,
    minWidth: 104,
    padding: spacing.sm,
  },
  fundamentalLabel: { ...typography.micro, color: colors.inkMuted },
  fundamentalValue: { ...typography.caption, color: colors.ink },
});
