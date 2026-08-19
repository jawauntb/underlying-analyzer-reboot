import { StyleSheet, Text, View } from 'react-native';

import { colors, radii, spacing, typography } from '@/src/theme/tokens';

export default function E2EFixtureBadge() {
  if (process.env.EXPO_PUBLIC_E2E_MODE !== '1') return null;
  return (
    <View
      accessibilityLabel="E2E fixture mode"
      style={styles.badge}
      testID="e2e-fixture-badge">
      <Text style={styles.text}>FIXTURE</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  badge: {
    backgroundColor: colors.coral,
    borderRadius: radii.pill,
    top: 52,
    paddingHorizontal: spacing.sm,
    paddingVertical: 4,
    pointerEvents: 'none',
    position: 'absolute',
    right: spacing.sm,
    zIndex: 1000,
  },
  text: { ...typography.micro, color: colors.graphite, fontWeight: '800' },
});
