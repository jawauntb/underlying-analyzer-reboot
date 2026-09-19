import { useMemo } from 'react';
import { StyleSheet, Text, useWindowDimensions, View } from 'react-native';
import Svg, { Circle, Path } from 'react-native-svg';

import { chartColors, colors, radii, spacing, typography } from '@/src/theme/tokens';

import type { ChartDataRow } from './ChartDataTable';
import { ChartFrame } from './ChartFrame';
import { ChartLegend, type ChartLegendItem } from './ChartLegend';
import { buildBarPath, buildLinePath, createLinearScale, finiteDomain } from './geometry';
import { normalizePeerForecastChart, type PeerForecastBar, type PeerForecastPair } from './models';

type PeerForecastChartProps = {
  dataset: unknown;
  fontScale?: number;
  title?: string;
  width?: number;
};

const ROW_HEIGHT = 22;
const BAR_HEIGHT = 12;
const SCATTER_HEIGHT = 170;
const LABEL_WIDTH = 58;
const VALUE_WIDTH = 96;

export function bucketLabel(bucket: string | null): string {
  if (!bucket) return 'Unavailable';
  return bucket.replace(/_/g, ' ').replace(/^\w/, (character) => character.toUpperCase());
}

export function formatSignedPercent(value: number | null): string {
  if (value === null) return 'Unavailable';
  const percent = value * 100;
  return `${percent >= 0 ? '+' : ''}${percent.toFixed(1)}%`;
}

export function formatConfidence(value: number | null): string {
  return value === null ? 'Unavailable' : `${Math.round(value * 100)}%`;
}

function pairColor(pair: PeerForecastPair): string {
  if (pair.isFocus) return chartColors.primary;
  return pair.predicted >= 0 ? chartColors.positive : chartColors.negative;
}

export function PeerForecastChart({
  dataset,
  fontScale: requestedFontScale,
  title = 'Peer forecast',
  width: requestedWidth,
}: PeerForecastChartProps) {
  const window = useWindowDimensions();
  const width = requestedWidth ?? window.width;
  const fontScale = requestedFontScale ?? window.fontScale;
  const compact = width < 350 || fontScale >= 1.3;
  const model = useMemo(() => normalizePeerForecastChart(dataset), [dataset]);

  const plotWidth = Math.max(40, width - LABEL_WIDTH - VALUE_WIDTH - spacing.md * 2);
  const barsHeight = Math.max(ROW_HEIGHT, model.ranked.length * ROW_HEIGHT);
  const xScale = createLinearScale(
    finiteDomain([0, ...model.ranked.map((bar) => bar.expectedExcessReturn)]),
    { min: 4, max: plotWidth - 4 },
  );
  const zero = xScale(0);
  const barMark = (bar: PeerForecastBar, index: number) => ({
    x: (zero + xScale(bar.expectedExcessReturn)) / 2,
    y: index * ROW_HEIGHT + (ROW_HEIGHT - BAR_HEIGHT) / 2,
    baseline: index * ROW_HEIGHT + (ROW_HEIGHT + BAR_HEIGHT) / 2,
    width: Math.abs(xScale(bar.expectedExcessReturn) - zero),
  });
  const positivePath = buildBarPath(
    model.ranked.flatMap((bar, index) => (bar.isFocus || bar.expectedExcessReturn < 0 ? [] : [barMark(bar, index)])),
  );
  const negativePath = buildBarPath(
    model.ranked.flatMap((bar, index) => (bar.isFocus || bar.expectedExcessReturn >= 0 ? [] : [barMark(bar, index)])),
  );
  const focusPath = buildBarPath(
    model.ranked.flatMap((bar, index) => (bar.isFocus ? [barMark(bar, index)] : [])),
  );
  const zeroPath = buildLinePath([{ x: zero, y: 0 }, { x: zero, y: barsHeight }]);

  const scatterInset = 12;
  const scatterWidth = Math.max(80, width - spacing.md * 2);
  const scatterDomain = finiteDomain(model.pairs.flatMap((pair) => [pair.predicted, pair.realized, 0]));
  const symmetric = Math.max(Math.abs(scatterDomain.min), Math.abs(scatterDomain.max)) || 1;
  const scatterX = createLinearScale({ min: -symmetric, max: symmetric }, { min: scatterInset, max: scatterWidth - scatterInset });
  const scatterY = createLinearScale({ min: -symmetric, max: symmetric }, { min: SCATTER_HEIGHT - scatterInset, max: scatterInset });
  const guidePath = buildLinePath([
    { x: scatterX(-symmetric), y: scatterY(-symmetric) },
    { x: scatterX(symmetric), y: scatterY(symmetric) },
  ]);

  const rows: ChartDataRow[] = model.ranked.map((bar) => {
    const pair = model.pairs.find((candidate) => candidate.symbol === bar.symbol);
    return {
      key: `peer-forecast-${bar.symbol}`,
      label: `${bar.rank}. ${bar.symbol}`,
      cells: [
        { label: 'Expected excess', value: formatSignedPercent(bar.expectedExcessReturn) },
        { label: 'Bucket', value: bucketLabel(bar.bucket) },
        { label: 'Confidence', value: formatConfidence(bar.confidence) },
        {
          label: 'Last predicted / realized',
          value: pair ? `${formatSignedPercent(pair.predicted)} / ${formatSignedPercent(pair.realized)}` : 'Unavailable',
        },
      ],
    };
  });

  const legendItems: ChartLegendItem[] = [
    { key: 'focus', label: model.ticker || 'Focus', color: chartColors.primary, mark: 'candle', spoken: 'highlighted bar' },
    { key: 'over', label: 'Above sector', color: chartColors.positive, mark: 'candle', spoken: 'bars to the right of zero' },
    { key: 'under', label: 'Below sector', color: chartColors.negative, mark: 'candle', spoken: 'bars to the left of zero' },
    ...(model.pairs.length
      ? [{ key: 'guide', label: 'Predicted = realized', color: chartColors.secondary, mark: 'dashed' as const, spoken: 'dashed guide line' }]
      : []),
  ];

  const headline = model.statesBucket
    ? `${bucketLabel(model.bucket)} · ${formatConfidence(model.confidence)}`
    : `Mixed · top ${bucketLabel(model.bucket).toLowerCase()} ${formatConfidence(model.confidence)}`;
  const subtitle = model.horizonMonths !== null && model.sectorEtf
    ? `${model.horizonMonths}m forward excess return vs ${model.sectorEtf}`
    : 'Forward excess return vs the sector ETF';

  return (
    <View style={styles.surface}>
      {model.ranked.length ? (
        <View style={styles.summary}>
          <View style={styles.summaryCopy}>
            <Text style={styles.eyebrow}>TABICL PEER FORECAST</Text>
            <Text accessibilityRole="header" style={styles.headline}>{headline}</Text>
            <Text style={styles.subtitle}>{subtitle}</Text>
          </View>
          <View style={styles.expected}>
            <Text style={styles.expectedLabel}>EXPECTED</Text>
            <Text style={styles.expectedValue}>{formatSignedPercent(model.expectedExcessReturn)}</Text>
          </View>
        </View>
      ) : null}

      <ChartFrame
        available={model.ranked.length > 0}
        data={rows}
        title={title}
        unavailableMessage="Peer forecast is unavailable."
        warnings={model.warnings.filter((warning) => warning !== 'Peer forecast is unavailable.')}>
        <View
          accessibilityElementsHidden
          importantForAccessibility="no-hide-descendants"
          style={styles.plot}
          testID={`${title}-plot`}>
          <View style={styles.rankedRow}>
            <View style={[styles.labels, { width: LABEL_WIDTH }]}>
              {model.ranked.map((bar) => (
                <Text
                  key={`label-${bar.symbol}`}
                  numberOfLines={1}
                  style={[styles.rowLabel, bar.isFocus && styles.rowLabelFocus, { height: ROW_HEIGHT }]}>
                  {bar.symbol}
                </Text>
              ))}
            </View>
            <Svg height={barsHeight} viewBox={`0 0 ${plotWidth} ${barsHeight}`} width={plotWidth}>
              {positivePath ? <Path d={positivePath} fill={chartColors.positive} /> : null}
              {negativePath ? <Path d={negativePath} fill={chartColors.negative} /> : null}
              {focusPath ? <Path d={focusPath} fill={chartColors.primary} /> : null}
              {zeroPath ? <Path d={zeroPath} fill="none" stroke={chartColors.grid} strokeWidth={1} /> : null}
            </Svg>
            <View style={[styles.labels, { width: VALUE_WIDTH }]}>
              {model.ranked.map((bar) => (
                <Text
                  key={`value-${bar.symbol}`}
                  numberOfLines={1}
                  style={[styles.rowValue, bar.isFocus && styles.rowLabelFocus, { height: ROW_HEIGHT }]}>
                  {formatSignedPercent(bar.expectedExcessReturn)} · {formatConfidence(bar.confidence)}
                </Text>
              ))}
            </View>
          </View>

          {model.pairs.length ? (
            <View style={styles.scatterBlock}>
              <Text style={styles.scatterTitle}>
                Predicted vs realized{model.lastLabeledDate ? ` · ${model.lastLabeledDate}` : ''}
              </Text>
              <Svg height={SCATTER_HEIGHT} viewBox={`0 0 ${scatterWidth} ${SCATTER_HEIGHT}`} width={scatterWidth}>
                {guidePath ? (
                  <Path d={guidePath} fill="none" stroke={chartColors.secondary} strokeDasharray="6 4" strokeWidth={1.5} />
                ) : null}
                <Path
                  d={`M${scatterInset} ${scatterY(0)}L${scatterWidth - scatterInset} ${scatterY(0)}M${scatterX(0)} ${scatterInset}L${scatterX(0)} ${SCATTER_HEIGHT - scatterInset}`}
                  fill="none"
                  stroke={chartColors.grid}
                  strokeWidth={1}
                />
                {model.pairs.map((pair) => (
                  <Circle
                    cx={scatterX(pair.predicted)}
                    cy={scatterY(pair.realized)}
                    fill={pairColor(pair)}
                    key={`pair-${pair.symbol}`}
                    r={pair.isFocus ? 6 : 3.5}
                    stroke={chartColors.surface}
                    strokeWidth={1}
                  />
                ))}
              </Svg>
              <View style={styles.scatterAxis}>
                <Text style={styles.axisLabel}>Predicted →</Text>
                <Text style={styles.axisLabel}>↑ Realized</Text>
              </View>
            </View>
          ) : null}
        </View>
      </ChartFrame>
      <ChartLegend items={legendItems} testID={`${title}-legend`} />
      {model.ranked.length && !compact ? (
        <Text style={styles.note}>
          In-context tabular model over the curated sector universe. A cross-check, not a call.
        </Text>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  surface: { gap: spacing.sm, width: '100%' },
  summary: {
    alignItems: 'flex-start',
    backgroundColor: colors.graphiteRaised,
    borderColor: colors.mineral,
    borderRadius: radii.md,
    borderWidth: 1,
    flexDirection: 'row',
    gap: spacing.md,
    justifyContent: 'space-between',
    padding: spacing.sm,
  },
  summaryCopy: { flex: 1, gap: 2 },
  eyebrow: { ...typography.micro, color: colors.cyan },
  headline: { ...typography.label, color: colors.ink },
  subtitle: { ...typography.caption, color: colors.inkSecondary },
  expected: { alignItems: 'flex-end', gap: 2 },
  expectedLabel: { ...typography.micro, color: colors.inkMuted },
  expectedValue: { ...typography.label, color: colors.mint },
  plot: { gap: spacing.sm, padding: spacing.sm },
  rankedRow: { alignItems: 'flex-start', flexDirection: 'row' },
  labels: { flexDirection: 'column' },
  rowLabel: { ...typography.micro, color: chartColors.muted, lineHeight: ROW_HEIGHT },
  rowLabelFocus: { color: colors.ink },
  rowValue: { ...typography.micro, color: colors.inkSecondary, lineHeight: ROW_HEIGHT, paddingLeft: spacing.xs },
  scatterBlock: { gap: spacing.xs },
  scatterTitle: { ...typography.micro, color: colors.inkMuted },
  scatterAxis: { flexDirection: 'row', justifyContent: 'space-between' },
  axisLabel: { ...typography.micro, color: chartColors.muted },
  note: { ...typography.caption, color: colors.inkMuted },
});
