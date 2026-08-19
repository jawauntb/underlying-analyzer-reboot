import * as Haptics from 'expo-haptics';
import { useEffect, useRef } from 'react';
import { type AccessibilityActionEvent, Pressable, StyleSheet, Text, View } from 'react-native';
import Svg, { Circle, Path } from 'react-native-svg';

import { colors, layout, radii, spacing, typography } from '@/src/theme/tokens';

import {
  moveResearchDepth,
  RESEARCH_DEPTH_DESCRIPTIONS,
  RESEARCH_DEPTH_LABELS,
  RESEARCH_DEPTHS,
  researchDepthAtPosition,
  type ResearchDepth,
} from './lens-model';

export type { ResearchDepth } from './lens-model';

type HapticsLike = Pick<typeof Haptics, 'selectionAsync'>;

type ResearchDepthDialProps = {
  selectedDepth: ResearchDepth;
  onChange: (depth: ResearchDepth) => void;
  width?: number;
  fontScale?: number;
  haptics?: HapticsLike;
};

const accessibilityActions = [
  { name: 'increment' as const, label: 'Increase research depth' },
  { name: 'decrement' as const, label: 'Decrease research depth' },
];

export default function ResearchDepthDial({
  selectedDepth,
  onChange,
  width = 375,
  fontScale = 1,
  haptics = Haptics,
}: ResearchDepthDialProps) {
  const dialWidth = Math.max(260, Math.min(390, width - 32));
  const dialHeight = Math.round(dialWidth * 0.56);
  const centerX = dialWidth / 2;
  const baseline = dialHeight - 18;
  const radius = centerX - 28;
  const compactText = fontScale >= 1.3;
  const selectedIndex = RESEARCH_DEPTHS.indexOf(selectedDepth);
  const selectedRef = useRef(selectedDepth);
  useEffect(() => {
    selectedRef.current = selectedDepth;
  }, [selectedDepth]);
  const selectedAngle = Math.PI - (selectedIndex * Math.PI) / 2;
  const selectedX = centerX + radius * Math.cos(selectedAngle);
  const selectedY = baseline - radius * Math.sin(selectedAngle);

  const select = (next: ResearchDepth) => {
    if (next === selectedRef.current) return;
    selectedRef.current = next;
    onChange(next);
    void haptics.selectionAsync().catch(() => undefined);
  };

  const onAccessibilityAction = (event: AccessibilityActionEvent) => {
    if (event.nativeEvent.actionName === 'increment') select(moveResearchDepth(selectedDepth, 1));
    if (event.nativeEvent.actionName === 'decrement') select(moveResearchDepth(selectedDepth, -1));
  };

  const selectAt = (locationX: number) => select(researchDepthAtPosition(locationX, dialWidth));

  return (
    <View style={styles.container}>
      <View
        accessible
        accessibilityActions={accessibilityActions}
        accessibilityHint="Swipe up or down to change depth. Choosing a depth does not start research."
        accessibilityLabel="Research depth"
        accessibilityRole="adjustable"
        accessibilityValue={{
          min: 1,
          max: 3,
          now: selectedIndex + 1,
          text: `${RESEARCH_DEPTH_LABELS[selectedDepth]}. ${RESEARCH_DEPTH_DESCRIPTIONS[selectedDepth]}`,
        }}
        onAccessibilityAction={onAccessibilityAction}
        onMoveShouldSetResponder={() => true}
        onResponderMove={(event) => selectAt(event.nativeEvent.locationX)}
        onResponderRelease={(event) => selectAt(event.nativeEvent.locationX)}
        onStartShouldSetResponder={() => true}
        style={[styles.instrument, { width: dialWidth }]}
        testID="depth-dial-gesture">
        <Svg height={dialHeight} viewBox={`0 0 ${dialWidth} ${dialHeight}`} width={dialWidth}>
          <Path
            d={`M${centerX - radius} ${baseline} A${radius} ${radius} 0 0 1 ${centerX + radius} ${baseline}`}
            fill="none"
            stroke={colors.mineral}
            strokeLinecap="round"
            strokeWidth={18}
          />
          <Path
            d={`M${centerX - radius} ${baseline} A${radius} ${radius} 0 0 1 ${centerX + radius} ${baseline}`}
            fill="none"
            stroke={colors.cyan}
            strokeDasharray={`${radius * Math.PI / 3 - 8} 8`}
            strokeLinecap="round"
            strokeWidth={3}
          />
          <Circle cx={selectedX} cy={selectedY} fill={colors.mint} r={11} stroke={colors.graphite} strokeWidth={4} />
        </Svg>
        <View style={[StyleSheet.absoluteFill, styles.detentLayer]}>
          {RESEARCH_DEPTHS.map((depth, index) => {
            const angle = Math.PI - (index * Math.PI) / 2;
            const x = centerX + radius * Math.cos(angle) - 22;
            const y = baseline - radius * Math.sin(angle) - 22;
            return (
              <Pressable
                accessibilityLabel={`Select ${RESEARCH_DEPTH_LABELS[depth]} on dial`}
                accessibilityRole="button"
                key={depth}
                onPress={() => select(depth)}
                style={({ pressed }) => [styles.detentTouch, { left: x, top: y }, pressed && styles.pressed]}>
                <View style={[styles.detentDot, depth === selectedDepth && styles.detentDotSelected]} />
              </Pressable>
            );
          })}
        </View>
      </View>

      <View style={styles.feedback} testID="depth-feedback">
        <Text style={styles.feedbackLabel}>Selected: {RESEARCH_DEPTH_LABELS[selectedDepth]}</Text>
        <Text style={styles.feedbackCopy}>{RESEARCH_DEPTH_DESCRIPTIONS[selectedDepth]}</Text>
        <Text style={styles.feedbackNote}>Selection is a preview. Nothing starts until you use the action below.</Text>
      </View>

      <View style={[styles.segments, compactText && styles.segmentsStacked]} testID="depth-segments">
        {RESEARCH_DEPTHS.map((depth) => {
          const selected = depth === selectedDepth;
          return (
            <Pressable
              accessibilityLabel={`Select ${RESEARCH_DEPTH_LABELS[depth]}`}
              accessibilityRole="button"
              accessibilityState={{ selected }}
              key={depth}
              onPress={() => select(depth)}
              style={({ pressed }) => [
                styles.segment,
                selected && styles.segmentSelected,
                compactText && styles.segmentStacked,
                pressed && styles.pressed,
              ]}>
              <Text style={[styles.segmentText, selected && styles.segmentTextSelected]}>
                {RESEARCH_DEPTH_LABELS[depth]}
              </Text>
            </Pressable>
          );
        })}
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { alignItems: 'center', gap: spacing.md, width: '100%' },
  instrument: { alignItems: 'center', alignSelf: 'center', position: 'relative' },
  detentLayer: { pointerEvents: 'box-none', position: 'absolute' },
  detentTouch: {
    alignItems: 'center',
    height: layout.minimumTouchTarget,
    justifyContent: 'center',
    position: 'absolute',
    width: layout.minimumTouchTarget,
  },
  detentDot: { backgroundColor: colors.mineral, borderRadius: 5, height: 10, width: 10 },
  detentDotSelected: { backgroundColor: colors.mint },
  feedback: {
    backgroundColor: colors.mineralSoft,
    borderRadius: radii.lg,
    gap: spacing.xs,
    padding: spacing.md,
    width: '100%',
  },
  feedbackLabel: { ...typography.title, color: colors.ink },
  feedbackCopy: { ...typography.body, color: colors.inkSecondary },
  feedbackNote: { ...typography.caption, color: colors.cyan },
  segments: { flexDirection: 'row', gap: spacing.xs, width: '100%' },
  segmentsStacked: { flexDirection: 'column' },
  segment: {
    alignItems: 'center',
    borderColor: colors.mineral,
    borderRadius: radii.md,
    borderWidth: 1,
    flex: 1,
    justifyContent: 'center',
    minHeight: layout.minimumTouchTarget,
    paddingHorizontal: spacing.sm,
    paddingVertical: spacing.sm,
  },
  segmentStacked: { flex: 0, width: '100%' },
  segmentSelected: { backgroundColor: colors.mint, borderColor: colors.mint },
  segmentText: { ...typography.caption, color: colors.inkSecondary, textAlign: 'center' },
  segmentTextSelected: { color: colors.graphite, fontWeight: '700' },
  pressed: { opacity: 0.72 },
});
