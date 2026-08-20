import { useMemo } from 'react';
import { StyleSheet, Text, useWindowDimensions, View } from 'react-native';
import Svg, { Path } from 'react-native-svg';

import { chartColors, spacing, typography } from '@/src/theme/tokens';

import type { ChartDataRow } from './ChartDataTable';
import { ChartFrame } from './ChartFrame';
import { ChartLegend, type ChartLegendItem } from './ChartLegend';
import { minMaxDecimate } from './decimate';
import { buildLinePath, computeChartLayout, createLinearScale, finiteDomain } from './geometry';
import type { LinePoint } from './models';

export type ChartLine = {
  key: string;
  label: string;
  color: string;
  dashArray?: string;
  /** Legend mark for this line. Defaults to a solid or dashed rule from `dashArray`. */
  mark?: ChartLegendItem['mark'];
  points: readonly LinePoint[];
  /** Spoken description of the mark for screen readers. */
  spoken?: string;
  width?: number;
};

type LineChartProps = {
  fontScale?: number;
  height?: number;
  lines: readonly ChartLine[];
  tableRows?: readonly ChartDataRow[];
  title: string;
  unavailableMessage?: string;
  warnings?: readonly string[];
  width?: number;
};

function normalizedLines(lines: readonly ChartLine[]): ChartLine[] {
  return lines.map((line) => ({
    ...line,
    points: line.points.filter(
      (point) => Number.isFinite(point.categoryIndex) && Number.isFinite(point.value) && Boolean(point.date),
    ),
  }));
}

function defaultTableRows(lines: readonly ChartLine[]): ChartDataRow[] {
  const categories = new Map<number, { date: string; values: Map<string, string> }>();
  lines.forEach((line) => {
    line.points.forEach((point) => {
      const category = categories.get(point.categoryIndex) ?? { date: point.date, values: new Map() };
      category.values.set(line.label, String(point.value));
      categories.set(point.categoryIndex, category);
    });
  });
  return [...categories.entries()]
    .sort(([left], [right]) => left - right)
    .map(([index, category]) => ({
      key: `line-${index}-${category.date}`,
      label: category.date,
      cells: [...category.values].map(([label, value]) => ({ label, value })),
    }));
}

export function LineChart({
  fontScale: requestedFontScale,
  height = 220,
  lines: inputLines,
  tableRows,
  title,
  unavailableMessage,
  warnings,
  width: requestedWidth,
}: LineChartProps) {
  const window = useWindowDimensions();
  const width = requestedWidth ?? window.width;
  const fontScale = requestedFontScale ?? window.fontScale;
  const lines = useMemo(() => normalizedLines(inputLines), [inputLines]);
  const allPoints = lines.flatMap((line) => line.points);
  const maximumCategory = allPoints.reduce((maximum, point) => Math.max(maximum, point.categoryIndex), 0);
  const pointCount = allPoints.length ? maximumCategory + 1 : 0;
  const chartLayout = computeChartLayout(width, height, fontScale, pointCount);
  const xScale = createLinearScale(finiteDomain(allPoints.map((point) => point.categoryIndex)), {
    min: chartLayout.plot.left,
    max: chartLayout.plot.right,
  });
  const yScale = createLinearScale(finiteDomain(allPoints.map((point) => point.value)), {
    min: chartLayout.plot.bottom,
    max: chartLayout.plot.top,
  });
  const renderBudget = Math.max(4, Math.floor(chartLayout.plot.width / 2));
  const rows = tableRows ? [...tableRows] : defaultTableRows(lines);
  const dateByIndex = new Map(allPoints.map((point) => [point.categoryIndex, point.date]));

  const legendItems: ChartLegendItem[] = lines
    .filter((line) => line.points.length)
    .map((line) => {
      const mark = line.mark ?? (line.dashArray ? 'dashed' : 'line');
      return {
        key: line.key,
        label: line.label,
        color: line.color,
        mark,
        spoken: line.spoken ?? `${mark === 'line' ? 'solid' : mark} line`,
      };
    });

  return (
    <View style={styles.surface}>
      <ChartFrame
        available={allPoints.length > 0}
        data={rows}
        title={title}
        unavailableMessage={unavailableMessage}
        warnings={warnings}>
        <View
          accessibilityElementsHidden
          importantForAccessibility="no-hide-descendants"
          testID={`${title}-plot`}>
          <Svg
            height={chartLayout.height}
            viewBox={`0 0 ${chartLayout.width} ${chartLayout.height}`}
            width={chartLayout.width}>
            {lines.map((line) => {
              const points = minMaxDecimate(line.points, renderBudget, (point) => point.value);
              const path = buildLinePath(points.map((point) => ({
                x: xScale(point.categoryIndex),
                y: yScale(point.value),
              })));
              return path ? (
                <Path
                  d={path}
                  fill="none"
                  key={line.key}
                  stroke={line.color}
                  strokeDasharray={line.dashArray}
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={line.width ?? 2}
                />
              ) : null;
            })}
          </Svg>
          <View style={styles.xLabels}>
            {chartLayout.xLabelIndices.map((index) => (
              <Text key={`${index}-${dateByIndex.get(index)}`} style={styles.axisLabel}>
                {dateByIndex.get(index) ?? ''}
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
