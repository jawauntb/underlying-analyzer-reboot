import { StyleSheet, Text, View } from 'react-native';
import Svg, { Line, Path } from 'react-native-svg';

import { colors, spacing, typography } from '@/src/theme/tokens';

export type ChartLegendItem = {
  key: string;
  label: string;
  color: string;
  /** Spoken description of the mark, e.g. "dashed line". Screen readers read it with the label. */
  spoken: string;
  mark: 'line' | 'dashed' | 'dotted' | 'dash-dot' | 'candle';
};

const SWATCH_WIDTH = 20;
const SWATCH_HEIGHT = 12;

const dashArrays: Record<ChartLegendItem['mark'], string | undefined> = {
  line: undefined,
  dashed: '5 4',
  dotted: '1.5 3',
  'dash-dot': '6 3 1.5 3',
  candle: undefined,
};

function Swatch({ color, mark }: { color: string; mark: ChartLegendItem['mark'] }) {
  const middle = SWATCH_HEIGHT / 2;
  return (
    <Svg height={SWATCH_HEIGHT} width={SWATCH_WIDTH}>
      {mark === 'candle' ? (
        <Path
          d={`M${SWATCH_WIDTH / 2} 0V${SWATCH_HEIGHT}M${SWATCH_WIDTH / 2 - 3.5} 3H${SWATCH_WIDTH / 2 + 3.5}V9H${SWATCH_WIDTH / 2 - 3.5}Z`}
          fill="none"
          stroke={color}
          strokeWidth={1.5}
        />
      ) : (
        <Line
          stroke={color}
          strokeDasharray={dashArrays[mark]}
          strokeWidth={mark === 'line' ? 1.75 : 2}
          x1={0}
          x2={SWATCH_WIDTH}
          y1={middle}
          y2={middle}
        />
      )}
    </Svg>
  );
}

/**
 * A compact, wrapping legend rail. Marks carry the pattern so the labels stay short;
 * the pattern still reaches screen readers through each item's accessibility label.
 * Item spacing uses margins rather than `gap` so a wrapped second line always adds
 * its own height to the rail instead of colliding with the content below it.
 */
export function ChartLegend({ items, testID }: { items: readonly ChartLegendItem[]; testID?: string }) {
  if (!items.length) return null;
  return (
    <View style={styles.legend} testID={testID}>
      {items.map((item) => (
        <View
          accessible
          accessibilityLabel={`${item.label}, ${item.spoken}`}
          accessibilityRole="text"
          key={item.key}
          style={styles.item}>
          <Swatch color={item.color} mark={item.mark} />
          <Text style={styles.label}>{item.label}</Text>
        </View>
      ))}
    </View>
  );
}

const styles = StyleSheet.create({
  legend: { flexDirection: 'row', flexWrap: 'wrap', marginBottom: -spacing.xs },
  item: {
    alignItems: 'center',
    flexDirection: 'row',
    marginBottom: spacing.xs,
    marginRight: spacing.md,
  },
  label: { ...typography.micro, color: colors.inkSecondary, marginLeft: 6 },
});
