import Constants from 'expo-constants';
import { useState } from 'react';
import { Pressable, ScrollView, StyleSheet, Switch, Text, useWindowDimensions, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { buildApiConfig } from '@/src/api/endpoints';
import AsyncState from '@/src/components/ui/AsyncState';
import { CHART_INTERVAL_CHIPS, RESEARCH_DEPTHS, RESEARCH_DEPTH_LABELS, type ResearchDepth } from '@/src/features/lens/lens-model';
import { AsyncCache } from '@/src/state/cache';
import { useNetworkReachability, type NetworkReachability } from '@/src/state/network';
import { usePreferences, type PreferencesContextValue } from '@/src/state/preferences';
import { colors, layout, radii, spacing, typography } from '@/src/theme/tokens';

const defaultCache = new AsyncCache();

type SettingsCache = Pick<AsyncCache, 'clear'>;

export type SettingsScreenProps = {
  cache?: SettingsCache;
  preferencesState?: PreferencesContextValue;
  reachability?: NetworkReachability;
  version?: string;
};

const REACHABILITY_COPY: Record<NetworkReachability, string> = {
  online: 'Online',
  offline: 'Offline · saved data only',
  unknown: 'Checking connectivity',
};

const DEPTH_DETAIL: Record<ResearchDepth, string> = {
  glance: 'Opens Torque and a 5d Auction.',
  diagnose: 'Adds Moneyline and the options pulse.',
  'deep-dive': 'Preselects the explicit Research Run.',
};

function ConnectedSettingsScreen(props: SettingsScreenProps) {
  const preferencesState = usePreferences();
  const reachability = useNetworkReachability();
  return <SettingsController {...props} preferencesState={preferencesState} reachability={reachability} />;
}

export default function SettingsScreen(props: SettingsScreenProps) {
  return props.preferencesState ? <SettingsController {...props} /> : <ConnectedSettingsScreen {...props} />;
}

function SettingsController({
  cache = defaultCache,
  preferencesState,
  reachability = 'unknown',
  version = Constants.expoConfig?.version ?? '0.0.0',
}: SettingsScreenProps) {
  const { width } = useWindowDimensions();
  const compact = width < 350;
  const [cacheState, setCacheState] = useState<{ status: 'idle' | 'clearing' | 'cleared' | 'error'; message?: string }>({ status: 'idle' });
  const [saveError, setSaveError] = useState<string | null>(null);
  const preferences = preferencesState?.preferences;
  const api = buildApiConfig();

  async function change(patch: Parameters<PreferencesContextValue['update']>[0]) {
    setSaveError(null);
    try {
      await preferencesState?.update(patch);
    } catch (error) {
      setSaveError(error instanceof Error && error.message ? error.message : 'That setting could not be saved.');
    }
  }

  async function clearCache() {
    setCacheState({ status: 'clearing' });
    try {
      await cache.clear();
      setCacheState({ status: 'cleared', message: 'Saved charts and research were cleared. Fresh data loads on the next request.' });
    } catch (error) {
      setCacheState({
        status: 'error',
        message: error instanceof Error && error.message ? error.message : 'Saved data could not be cleared.',
      });
    }
  }

  return (
    <SafeAreaView edges={['top', 'left', 'right']} style={styles.safeArea}>
      <ScrollView contentContainerStyle={[styles.content, compact && styles.compactContent]} contentInsetAdjustmentBehavior="automatic">
        <Text style={styles.eyebrow}>THIS DEVICE</Text>
        <Text accessibilityRole="header" style={styles.title}>Settings</Text>
        <Text style={styles.intro}>Defaults for how the app opens, and what it keeps on this device.</Text>

        {preferencesState?.error ? (
          <AsyncState message={preferencesState.error} title="Saved settings unavailable" tone="warning" />
        ) : null}

        <View style={styles.section}>
          <Text style={styles.sectionEyebrow}>CHARTS</Text>
          <Text style={styles.sectionTitle}>Opening interval</Text>
          <Text style={styles.sectionCopy}>Every chart opens on this interval until you change it in the Lens.</Text>
          <View accessibilityRole="tablist" style={styles.rail}>
            {CHART_INTERVAL_CHIPS.map((candidate) => {
              const active = preferences?.defaultInterval === candidate.value;
              return (
                <Pressable
                  accessibilityLabel={`Open charts on the ${candidate.spoken} interval`}
                  accessibilityRole="tab"
                  accessibilityState={{ selected: active }}
                  key={candidate.value}
                  onPress={() => void change({ defaultInterval: candidate.value })}
                  style={({ pressed }) => [styles.segment, active && styles.segmentActive, pressed && styles.pressed]}>
                  <Text numberOfLines={1} style={[styles.segmentText, active && styles.segmentTextActive]}>{candidate.label}</Text>
                </Pressable>
              );
            })}
          </View>
        </View>

        <View style={styles.section}>
          <Text style={styles.sectionEyebrow}>RESEARCH</Text>
          <Text style={styles.sectionTitle}>Preselected depth</Text>
          <Text style={styles.sectionCopy}>
            {preferences ? DEPTH_DETAIL[preferences.defaultDepth] : DEPTH_DETAIL.glance} Research still opens only when you ask for it.
          </Text>
          <View accessibilityRole="tablist" style={styles.rail}>
            {RESEARCH_DEPTHS.map((depth) => {
              const active = preferences?.defaultDepth === depth;
              return (
                <Pressable
                  accessibilityLabel={`Preselect ${RESEARCH_DEPTH_LABELS[depth]}`}
                  accessibilityRole="tab"
                  accessibilityState={{ selected: active }}
                  key={depth}
                  onPress={() => void change({ defaultDepth: depth })}
                  style={({ pressed }) => [styles.segment, active && styles.segmentActive, pressed && styles.pressed]}>
                  <Text numberOfLines={1} style={[styles.segmentText, active && styles.segmentTextActive]}>
                    {RESEARCH_DEPTH_LABELS[depth]}
                  </Text>
                </Pressable>
              );
            })}
          </View>
        </View>

        <View style={styles.section}>
          <Text style={styles.sectionEyebrow}>DATA</Text>
          <View style={styles.switchRow}>
            <View style={styles.switchCopy}>
              <Text style={styles.sectionTitle}>Live quote card</Text>
              <Text style={styles.sectionCopy}>Spends one extra snapshot request each time a Lens opens.</Text>
            </View>
            <Switch
              accessibilityLabel="Live quote card"
              ios_backgroundColor={colors.mineral}
              onValueChange={(next) => void change({ liveQuotes: next })}
              thumbColor={colors.ink}
              trackColor={{ false: colors.mineral, true: colors.mint }}
              value={preferences?.liveQuotes ?? true}
            />
          </View>
          {saveError ? <Text accessibilityRole="alert" style={styles.error}>{saveError}</Text> : null}
          <Pressable
            accessibilityLabel="Clear saved data"
            accessibilityRole="button"
            accessibilityState={{ busy: cacheState.status === 'clearing' }}
            onPress={() => void clearCache()}
            style={({ pressed }) => [styles.secondaryAction, pressed && styles.pressed]}>
            <Text style={styles.secondaryActionText}>
              {cacheState.status === 'clearing' ? 'Clearing…' : 'Clear saved data'}
            </Text>
          </Pressable>
          {cacheState.message ? (
            <Text
              accessibilityRole={cacheState.status === 'error' ? 'alert' : 'text'}
              style={cacheState.status === 'error' ? styles.error : styles.success}>
              {cacheState.message}
            </Text>
          ) : null}
        </View>

        <View style={styles.section}>
          <Text style={styles.sectionEyebrow}>DIAGNOSTICS</Text>
          <Row label="CONNECTION" value={REACHABILITY_COPY[reachability]} />
          <Row label="API ORIGIN" value={api.status === 'configured' ? api.baseUrl : api.message} />
          <Row label="APP VERSION" value={version} />
        </View>
      </ScrollView>
    </SafeAreaView>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <View accessibilityLabel={`${label}: ${value}`} style={styles.row}>
      <Text style={styles.rowLabel}>{label}</Text>
      <Text style={styles.rowValue}>{value}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  safeArea: { backgroundColor: colors.graphite, flex: 1 },
  content: {
    alignSelf: 'center',
    gap: spacing.lg,
    maxWidth: layout.maximumContentWidth,
    paddingBottom: spacing.xxxl,
    paddingHorizontal: spacing.lg,
    paddingTop: spacing.md,
    width: '100%',
  },
  compactContent: { paddingHorizontal: spacing.md },
  eyebrow: { ...typography.eyebrow, color: colors.cyan },
  title: { ...typography.display, color: colors.ink, marginTop: -spacing.sm },
  intro: { ...typography.body, color: colors.inkSecondary, marginTop: -spacing.md },
  section: {
    backgroundColor: colors.graphiteRaised,
    borderColor: colors.mineral,
    borderRadius: radii.xl,
    borderWidth: 1,
    gap: spacing.sm,
    padding: spacing.md,
  },
  sectionEyebrow: { ...typography.eyebrow, color: colors.cyan },
  sectionTitle: { ...typography.title, color: colors.ink },
  sectionCopy: { ...typography.caption, color: colors.inkSecondary },
  rail: { backgroundColor: colors.mineralSoft, borderRadius: radii.pill, flexDirection: 'row', padding: 3 },
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
  switchRow: { alignItems: 'center', flexDirection: 'row', gap: spacing.md, justifyContent: 'space-between' },
  switchCopy: { flex: 1, gap: spacing.xs },
  secondaryAction: {
    alignItems: 'center',
    borderColor: colors.mineral,
    borderRadius: radii.md,
    borderWidth: 1,
    justifyContent: 'center',
    minHeight: layout.minimumTouchTarget,
    paddingHorizontal: spacing.md,
  },
  secondaryActionText: { ...typography.label, color: colors.ink },
  row: { borderTopColor: colors.mineral, borderTopWidth: 1, gap: 2, paddingTop: spacing.sm },
  rowLabel: { ...typography.micro, color: colors.inkMuted },
  rowValue: { ...typography.caption, color: colors.ink },
  error: { ...typography.caption, color: colors.coral },
  success: { ...typography.caption, color: colors.mint },
  pressed: { opacity: 0.72 },
});
