import { StyleSheet, Text, useWindowDimensions, View } from 'react-native';
import Svg, { Path } from 'react-native-svg';

import { chartColors, spacing, typography } from '@/src/theme/tokens';

import type { ChartDataRow } from './ChartDataTable';
import { ChartFrame } from './ChartFrame';
import { ChartLegend, type ChartLegendItem } from './ChartLegend';
import { buildBarPath, buildLinePath, computeChartLayout, createLinearScale, finiteDomain } from './geometry';
import { normalizeMoneylineChart } from './models';

type MoneylineChartProps = {
  dataset: unknown;
  fontScale?: number;
  height?: number;
  title?: string;
  width?: number;
};

function displayRatio(value: number | null): string {
  return value === null ? 'Unavailable' : value.toFixed(2);
}

export function MoneylineChart({
  dataset,
  fontScale: requestedFontScale,
  height = 220,
  title = 'Moneyline',
  width: requestedWidth,
}: MoneylineChartProps) {
  const window = useWindowDimensions();
  const width = requestedWidth ?? window.width;
  const fontScale = requestedFontScale ?? window.fontScale;
  const model = normalizeMoneylineChart(dataset);
  const chartLayout = computeChartLayout(width, height, fontScale, model.data.length);
  const xScale = createLinearScale(finiteDomain(model.data.map((_, index) => index)), {
    min: chartLayout.plot.left,
    max: chartLayout.plot.right,
  });
  const yScale = createLinearScale(
    finiteDomain(model.data.flatMap((point) => [point.callOpenInterest, -point.putOpenInterest, 0])),
    { min: chartLayout.plot.bottom, max: chartLayout.plot.top },
  );
  const baseline = yScale(0);
  const barWidth = Math.max(1, Math.min(12, chartLayout.plot.width / Math.max(model.data.length, 1) / 2.6));
  const callPath = buildBarPath(model.data.map((point, index) => ({
    x: xScale(index) - barWidth / 2,
    y: yScale(point.callOpenInterest),
    baseline,
    width: barWidth,
  })));
  const putPath = buildBarPath(model.data.map((point, index) => ({
    x: xScale(index) + barWidth / 2,
    y: yScale(-point.putOpenInterest),
    baseline,
    width: barWidth,
  })));
  const zeroPath = buildLinePath([
    { x: chartLayout.plot.left, y: baseline },
    { x: chartLayout.plot.right, y: baseline },
  ]);
  const closestSpotIndex = model.currentPrice === null || !model.data.length ? null : model.data.reduce(
    (best, point, index) =>
      Math.abs(point.strike - model.currentPrice!) < Math.abs(model.data[best].strike - model.currentPrice!) ? index : best,
    0,
  );
  const rows: ChartDataRow[] = model.data.map((point) => ({
    key: `moneyline-${point.strike}`,
    label: String(point.strike),
    cells: [{
      label: 'Open interest',
      value: `Calls ${point.callOpenInterest} · Puts ${point.putOpenInterest} · Net ${point.netOpenInterest ?? 'Unavailable'} · P/C ${displayRatio(point.putCallRatio)}`,
    }],
  }));

  const legendItems: ChartLegendItem[] = [
    { key: 'calls', label: 'Calls', color: chartColors.positive, mark: 'candle', spoken: 'bars above zero' },
    { key: 'puts', label: 'Puts', color: chartColors.negative, mark: 'candle', spoken: 'bars below zero' },
    ...(model.currentPrice === null
      ? []
      : [{ key: 'spot', label: 'Spot', color: chartColors.secondary, mark: 'dashed' as const, spoken: 'dashed line' }]),
  ];

  return (
    <View style={styles.surface}>
      <ChartFrame
        available={model.positioningAvailable}
        data={rows}
        title={title}
        unavailableMessage="Options positioning is unavailable."
        warnings={model.warnings.filter((warning) => warning !== 'Options positioning is unavailable.')}>
        <View
          accessibilityElementsHidden
          importantForAccessibility="no-hide-descendants"
          testID={`${title}-plot`}>
          <Svg
            height={chartLayout.height}
            viewBox={`0 0 ${chartLayout.width} ${chartLayout.height}`}
            width={chartLayout.width}>
            {callPath ? <Path d={callPath} fill={chartColors.positive} /> : null}
            {putPath ? <Path d={putPath} fill={chartColors.negative} /> : null}
            {zeroPath ? <Path d={zeroPath} fill="none" stroke={chartColors.primary} strokeWidth={1.5} /> : null}
            {closestSpotIndex === null ? null : (
              <Path
                d={`M${xScale(closestSpotIndex)} ${chartLayout.plot.top}L${xScale(closestSpotIndex)} ${chartLayout.plot.bottom}`}
                fill="none"
                stroke={chartColors.secondary}
                strokeDasharray="6 4"
                strokeWidth={1.5}
              />
            )}
          </Svg>
          <View style={styles.xLabels}>
            {chartLayout.xLabelIndices.map((index) => (
              <Text key={`${index}-${model.data[index]?.strike}`} style={styles.axisLabel}>
                {model.data[index]?.strike ?? ''}
              </Text>
            ))}
          </View>
        </View>
      </ChartFrame>
      <ChartLegend items={legendItems} testID={`${title}-legend`} />
    </View>
  );
}

const styles = StyleSheet.create({
  surface: { gap: spacing.sm, width: '100%' },
  xLabels: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    paddingBottom: spacing.xs,
    paddingHorizontal: spacing.sm,
  },
  axisLabel: { ...typography.micro, color: chartColors.muted, flexShrink: 1 },
});
