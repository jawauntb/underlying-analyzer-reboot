import { StyleSheet, Text, useWindowDimensions, View } from 'react-native';
import Svg, { Path } from 'react-native-svg';

import { chartColors, colors, spacing, typography } from '@/src/theme/tokens';

import type { ChartDataRow } from './ChartDataTable';
import { ChartFrame } from './ChartFrame';
import { aggregateOhlcv } from './decimate';
import {
  buildCandlePaths,
  buildLinePath,
  computeChartLayout,
  createLinearScale,
  finiteDomain,
} from './geometry';
import { normalizeAuctionChart } from './models';

type AuctionChartProps = {
  dataset: unknown;
  fontScale?: number;
  height?: number;
  title?: string;
  width?: number;
};

const levelSpecs = [
  { key: 'vah' as const, label: 'VAH — dashed', color: chartColors.positive, dashArray: '7 5' },
  { key: 'val' as const, label: 'VAL — dashed', color: chartColors.negative, dashArray: '7 5' },
  { key: 'poc' as const, label: 'POC — dash-dot', color: chartColors.secondary, dashArray: '9 4 2 4' },
];

export function AuctionChart({
  dataset,
  fontScale: requestedFontScale,
  height = 220,
  title = 'Auction',
  width: requestedWidth,
}: AuctionChartProps) {
  const window = useWindowDimensions();
  const width = requestedWidth ?? window.width;
  const fontScale = requestedFontScale ?? window.fontScale;
  const model = normalizeAuctionChart(dataset);
  const chartLayout = computeChartLayout(width, height, fontScale, model.data.length);
  const aggregated = aggregateOhlcv(model.data, Math.max(1, Math.floor(chartLayout.plot.width / 6)));
  const levelValues = Object.values(model.levels).filter((value): value is number => value !== null);
  const domain = finiteDomain([
    ...model.data.flatMap((point) => [point.high, point.low, point.open, point.close]),
    ...levelValues,
  ]);
  const xScale = createLinearScale(finiteDomain(model.data.map((point) => point.categoryIndex)), {
    min: chartLayout.plot.left,
    max: chartLayout.plot.right,
  });
  const yScale = createLinearScale(domain, { min: chartLayout.plot.bottom, max: chartLayout.plot.top });
  const candleWidth = Math.max(1, Math.min(8, chartLayout.plot.width / Math.max(aggregated.length, 1) - 1));
  const candlePaths = buildCandlePaths(aggregated.map((point) => ({
    x: xScale((point.sourceStartIndex + point.sourceEndIndex) / 2),
    open: yScale(point.open),
    high: yScale(point.high),
    low: yScale(point.low),
    close: yScale(point.close),
    width: candleWidth,
  })));
  const closePath = buildLinePath(aggregated.map((point) => ({
    x: xScale((point.sourceStartIndex + point.sourceEndIndex) / 2),
    y: yScale(point.close),
  })));
  const rows: ChartDataRow[] = [
    ...model.data.map((point) => ({
      key: `auction-${point.categoryIndex}-${point.date}`,
      label: point.date,
      cells: [{
        label: 'OHLCV',
        value: `O ${point.open} · H ${point.high} · L ${point.low} · C ${point.close} · V ${point.volume}`,
      }],
    })),
    ...levelSpecs.flatMap((level) => {
      const value = model.levels[level.key];
      return value === null ? [] : [{
        key: `auction-level-${level.key}`,
        label: level.key.toUpperCase(),
        cells: [{ label: 'Level', value: String(value) }],
      }];
    }),
  ];

  return (
    <View style={styles.surface}>
      <View testID={`${title}-legend`} style={[styles.legend, chartLayout.compact && styles.legendCompact]}>
        <Text style={styles.legendText}>Candles ▲ up / ▼ down</Text>
        <Text style={styles.legendText}>Close — solid</Text>
        {levelSpecs.filter((level) => model.levels[level.key] !== null).map((level) => (
          <Text key={level.key} style={styles.legendText}>{level.label}</Text>
        ))}
      </View>
      <ChartFrame available={model.data.length > 0} data={rows} title={title} warnings={model.warnings}>
        <View
          accessibilityElementsHidden
          importantForAccessibility="no-hide-descendants"
          testID={`${title}-plot`}>
          <Svg
            height={chartLayout.height}
            viewBox={`0 0 ${chartLayout.width} ${chartLayout.height}`}
            width={chartLayout.width}>
            {candlePaths.up ? <Path d={candlePaths.up} fill="none" stroke={chartColors.positive} strokeWidth={1.5} /> : null}
            {candlePaths.down ? <Path d={candlePaths.down} fill="none" stroke={chartColors.negative} strokeWidth={1.5} /> : null}
            {closePath ? <Path d={closePath} fill="none" stroke={chartColors.primary} strokeWidth={2} /> : null}
            {levelSpecs.map((level) => {
              const value = model.levels[level.key];
              if (value === null) return null;
              const y = yScale(value);
              return (
                <Path
                  d={`M${chartLayout.plot.left} ${y}L${chartLayout.plot.right} ${y}`}
                  fill="none"
                  key={level.key}
                  stroke={level.color}
                  strokeDasharray={level.dashArray}
                  strokeWidth={level.key === 'poc' ? 2 : 1.25}
                />
              );
            })}
          </Svg>
          <View style={styles.xLabels}>
            {chartLayout.xLabelIndices.map((index) => (
              <Text key={`${index}-${model.data[index]?.date}`} style={styles.axisLabel}>
                {model.data[index]?.date ?? ''}
              </Text>
            ))}
          </View>
        </View>
      </ChartFrame>
    </View>
  );
}

const styles = StyleSheet.create({
  surface: { gap: spacing.sm, width: '100%' },
  legend: { flexDirection: 'row', flexWrap: 'wrap', gap: spacing.sm },
  legendCompact: { alignItems: 'flex-start', flexDirection: 'column' },
  legendText: { ...typography.caption, color: colors.inkSecondary },
  xLabels: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    paddingBottom: spacing.xs,
    paddingHorizontal: spacing.sm,
  },
  axisLabel: { ...typography.micro, color: chartColors.muted, flexShrink: 1 },
});
