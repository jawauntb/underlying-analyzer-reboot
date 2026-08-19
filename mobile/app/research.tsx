import * as Haptics from 'expo-haptics';
import { useLocalSearchParams, useRouter } from 'expo-router';
import { Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import ResearchRunScreen from '@/src/features/research/ResearchRunScreen';
import { normalizeResearchRouteParams } from '@/src/features/research/research-model';
import { colors, layout, radii, spacing, typography } from '@/src/theme/tokens';

export default function ResearchScreen() {
  const params = useLocalSearchParams<{
    symbol?: string | string[];
    period?: string | string[];
    recordId?: string | string[];
  }>();
  const route = normalizeResearchRouteParams(params);
  if (!route.ok) return <InvalidResearchRoute error={route.error} />;
  return <ResearchRunScreen period={route.period} recordId={route.recordId} symbol={route.symbol} />;
}

function InvalidResearchRoute({ error }: { error: string }) {
  const router = useRouter();
  const close = () => {
    void Haptics.selectionAsync().catch(() => undefined);
    router.back();
  };
  return (
    <SafeAreaView edges={['bottom', 'left', 'right']} style={styles.safeArea}>
      <ScrollView contentContainerStyle={styles.content} contentInsetAdjustmentBehavior="automatic">
        <View accessibilityLabel="Research Run preview placeholder" style={styles.tag}>
          <Text style={styles.tagText}>RESEARCH ROUTE</Text>
        </View>
        <Text accessibilityRole="header" style={styles.title}>Research Run unavailable</Text>
        <View accessibilityRole="alert" style={styles.errorCard}><Text style={styles.errorText}>{error}</Text></View>
        <Text style={styles.body}>Return to a Ticker Lens and choose Deep Dive. No capability check or research request was made.</Text>
        <Pressable accessibilityLabel="Close Research Run preview" accessibilityRole="button" onPress={close} style={({ pressed }) => [styles.closeAction, pressed && styles.pressed]}>
          <Text style={styles.closeText}>Close</Text>
        </Pressable>
      </ScrollView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safeArea: { backgroundColor: colors.graphiteRaised, flex: 1 },
  content: { alignSelf: 'center', gap: spacing.md, maxWidth: layout.maximumContentWidth, padding: spacing.lg, paddingBottom: spacing.xxxl, width: '100%' },
  tag: { alignItems: 'center', alignSelf: 'flex-start', backgroundColor: colors.mineralSoft, borderRadius: radii.pill, justifyContent: 'center', minHeight: layout.minimumTouchTarget, paddingHorizontal: spacing.sm },
  tagText: { ...typography.micro, color: colors.coral },
  title: { ...typography.display, color: colors.ink },
  errorCard: { borderColor: colors.coral, borderRadius: radii.lg, borderWidth: 1, padding: spacing.md },
  errorText: { ...typography.body, color: colors.coral },
  body: { ...typography.body, color: colors.inkSecondary },
  closeAction: { alignItems: 'center', borderColor: colors.mineral, borderRadius: radii.md, borderWidth: 1, justifyContent: 'center', minHeight: layout.minimumTouchTarget, paddingHorizontal: spacing.md, paddingVertical: spacing.sm },
  closeText: { ...typography.label, color: colors.ink },
  pressed: { opacity: 0.72 },
});
