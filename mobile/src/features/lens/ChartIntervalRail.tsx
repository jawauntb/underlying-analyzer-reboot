import { Pressable, StyleSheet, Text, View } from 'react-native';

import { colors, layout, radii, spacing, typography } from '@/src/theme/tokens';

import { CHART_INTERVAL_CHIPS, type ChartInterval } from './lens-model';

type ChartIntervalRailProps = {
  interval: ChartInterval;
  onChange(next: ChartInterval): void;
  testID?: string;
};

/**
 * The interval selector, as one fixed row of equal segments. Segments are sized by
 * flex rather than wrapped, so the rail keeps a single predictable height at every
 * width and font scale and never lands on top of the chart meta beneath it.
 */
export default function ChartIntervalRail({ interval, onChange, testID }: ChartIntervalRailProps) {
  return (
    <View accessibilityRole="tablist" style={styles.rail} testID={testID}>
      {CHART_INTERVAL_CHIPS.map((candidate) => {
        const active = candidate.value === interval;
        return (
          <Pressable
            accessibilityLabel={`Show ${candidate.spoken} interval`}
            accessibilityRole="tab"
            accessibilityState={{ selected: active }}
            key={candidate.value}
            onPress={() => onChange(candidate.value)}
            style={({ pressed }) => [styles.segment, active && styles.segmentActive, pressed && styles.pressed]}>
            <Text numberOfLines={1} style={[styles.segmentText, active && styles.segmentTextActive]}>
              {candidate.label}
            </Text>
          </Pressable>
        );
      })}
    </View>
  );
}

const styles = StyleSheet.create({
  rail: {
    backgroundColor: colors.mineralSoft,
    borderRadius: radii.pill,
    flexDirection: 'row',
    padding: 3,
  },
  segment: {
    alignItems: 'center',
    borderRadius: radii.pill,
    flex: 1,
    justifyContent: 'center',
    minHeight: layout.minimumTouchTarget,
    paddingHorizontal: spacing.xs,
  },
  segmentActive: { backgroundColor: colors.mint },
  segmentText: { ...typography.micro, color: colors.inkSecondary },
  segmentTextActive: { color: colors.graphite },
  pressed: { opacity: 0.72 },
});
