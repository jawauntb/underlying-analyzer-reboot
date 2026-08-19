import type { ReactNode } from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';

import { colors, layout, radii, spacing, typography } from '@/src/theme/tokens';

type AsyncStateProps = {
  accessibilityLabel?: string;
  title: string;
  message: string;
  actionLabel?: string;
  actionDisabled?: boolean;
  onAction?: () => void;
  tone?: 'neutral' | 'warning' | 'error';
  children?: ReactNode;
};

export default function AsyncState({
  accessibilityLabel,
  title,
  message,
  actionLabel,
  actionDisabled = false,
  onAction,
  tone = 'neutral',
  children,
}: AsyncStateProps) {
  return (
    <View accessibilityLabel={accessibilityLabel} style={[styles.container, styles[`${tone}Tone`]]}>
      <Text style={styles.title}>{title}</Text>
      <Text style={styles.message}>{message}</Text>
      {children}
      {actionLabel && onAction ? (
        <Pressable
          accessibilityLabel={actionLabel}
          accessibilityRole="button"
          accessibilityState={{ disabled: actionDisabled }}
          disabled={actionDisabled}
          onPress={onAction}
          style={({ pressed }) => [styles.action, actionDisabled && styles.disabled, pressed && styles.pressed]}>
          <Text style={styles.actionText}>{actionLabel}</Text>
        </Pressable>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    backgroundColor: colors.graphiteRaised,
    borderColor: colors.mineral,
    borderRadius: radii.lg,
    borderWidth: 1,
    gap: spacing.sm,
    padding: spacing.md,
  },
  neutralTone: {},
  warningTone: { borderColor: colors.cyan },
  errorTone: { borderColor: colors.coral },
  title: { ...typography.title, color: colors.ink },
  message: { ...typography.body, color: colors.inkSecondary },
  action: {
    alignItems: 'center',
    alignSelf: 'flex-start',
    borderColor: colors.mineral,
    borderRadius: radii.md,
    borderWidth: 1,
    justifyContent: 'center',
    minHeight: layout.minimumTouchTarget,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.sm,
  },
  actionText: { ...typography.label, color: colors.ink },
  disabled: { opacity: 0.45 },
  pressed: { opacity: 0.72 },
});
