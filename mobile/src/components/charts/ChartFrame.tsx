import { useEffect, useMemo, useState, type ReactNode } from 'react';
import {
  type AccessibilityActionEvent,
  Pressable,
  StyleSheet,
  Text,
  View,
} from 'react-native';

import { colors, layout, radii, spacing, typography } from '@/src/theme/tokens';

import { ChartDataTable, chartRowAccessibilityText, type ChartDataRow } from './ChartDataTable';

type ChartFrameProps = {
  available?: boolean;
  children?: ReactNode;
  data: readonly ChartDataRow[];
  title: string;
  unavailableMessage?: string;
  warnings?: readonly string[];
};

const accessibilityActions = [
  { name: 'increment' as const, label: 'Next value' },
  { name: 'decrement' as const, label: 'Previous value' },
  { name: 'activate' as const, label: 'View all data' },
];

export function ChartFrame({
  available = true,
  children,
  data,
  title,
  unavailableMessage = 'Chart data unavailable.',
  warnings = [],
}: ChartFrameProps) {
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [tableVisible, setTableVisible] = useState(false);
  const enabled = available && data.length > 0;

  useEffect(() => {
    setSelectedIndex((index) => Math.min(index, Math.max(0, data.length - 1)));
  }, [data.length]);

  const selectedText = useMemo(
    () => data[selectedIndex] ? chartRowAccessibilityText(data[selectedIndex]) : unavailableMessage,
    [data, selectedIndex, unavailableMessage],
  );

  const moveSelection = (offset: number) => {
    if (!data.length) return;
    setSelectedIndex((index) => Math.max(0, Math.min(data.length - 1, index + offset)));
  };

  const onAccessibilityAction = (event: AccessibilityActionEvent) => {
    switch (event.nativeEvent.actionName) {
      case 'increment':
        moveSelection(1);
        break;
      case 'decrement':
        moveSelection(-1);
        break;
      case 'activate':
        if (data.length) setTableVisible(true);
        break;
    }
  };

  return (
    <View style={styles.frame}>
      <View
        accessible
        accessibilityActions={accessibilityActions}
        accessibilityHint="Swipe up or down to move through values, or activate to view all data."
        accessibilityLabel={`${title} chart`}
        accessibilityRole="adjustable"
        accessibilityState={{ disabled: !enabled }}
        accessibilityValue={{ text: selectedText }}
        onAccessibilityAction={onAccessibilityAction}
        style={styles.adjustable}>
        {enabled ? children : <Text style={styles.unavailable}>{unavailableMessage}</Text>}
      </View>

      {warnings.map((warning) => (
        <Text accessibilityRole="alert" key={warning} style={styles.warning}>
          {warning}
        </Text>
      ))}

      <Pressable
        accessibilityLabel={`View ${title} data`}
        accessibilityRole="button"
        disabled={!data.length}
        onPress={() => setTableVisible(true)}
        style={({ pressed }) => [styles.dataButton, pressed && styles.pressed, !data.length && styles.disabled]}>
        <Text style={styles.dataButtonText}>View data</Text>
      </Pressable>

      <ChartDataTable
        onClose={() => setTableVisible(false)}
        rows={data}
        title={title}
        visible={tableVisible}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  frame: { gap: spacing.sm, width: '100%' },
  adjustable: {
    backgroundColor: colors.graphiteRaised,
    borderColor: colors.mineral,
    borderRadius: radii.lg,
    borderWidth: 1,
    overflow: 'hidden',
    width: '100%',
  },
  unavailable: { ...typography.body, color: colors.inkSecondary, padding: spacing.lg },
  warning: { ...typography.caption, color: colors.coral },
  dataButton: {
    alignItems: 'center',
    alignSelf: 'flex-start',
    borderColor: colors.mineral,
    borderRadius: radii.pill,
    borderWidth: 1,
    justifyContent: 'center',
    minHeight: layout.minimumTouchTarget,
    paddingHorizontal: spacing.md,
  },
  dataButtonText: { ...typography.label, color: colors.cyan },
  pressed: { opacity: 0.72 },
  disabled: { opacity: 0.48 },
});
