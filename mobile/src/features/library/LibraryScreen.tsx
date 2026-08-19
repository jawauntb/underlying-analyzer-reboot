import Ionicons from '@expo/vector-icons/Ionicons';
import * as Haptics from 'expo-haptics';
import { useIsFocused } from '@react-navigation/native';
import { useRouter, type Href } from 'expo-router';
import { useEffect, useRef, useState } from 'react';
import { Pressable, ScrollView, StyleSheet, Text, useWindowDimensions, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { colors, layout, radii, spacing, typography } from '@/src/theme/tokens';

import { defaultLibraryStore, type LibraryRecord, type LibraryStore } from './library-store';

type LibraryDataSource = Pick<LibraryStore, 'list' | 'delete' | 'clear'>;
type LibraryRouter = { push(href: Href): void };
type LibraryHaptics = Pick<typeof Haptics, 'selectionAsync' | 'notificationAsync'>;

export type LibraryScreenProps = {
  store?: LibraryDataSource;
  router?: LibraryRouter;
  haptics?: LibraryHaptics;
  focused?: boolean;
  width?: number;
};

function message(error: unknown): string {
  return error instanceof Error && error.message ? error.message : 'The on-device Library could not be opened.';
}

function ConnectedLibraryScreen(props: LibraryScreenProps) {
  const router = useRouter();
  const focused = useIsFocused();
  const { width } = useWindowDimensions();
  return <LibraryController {...props} focused={focused} router={router} width={width} />;
}

export default function LibraryScreen(props: LibraryScreenProps) {
  const injected = props.router !== undefined && props.focused !== undefined && props.width !== undefined;
  return injected ? <LibraryController {...props} /> : <ConnectedLibraryScreen {...props} />;
}

function LibraryController({
  store = defaultLibraryStore,
  router = { push: () => undefined },
  haptics = Haptics,
  focused = true,
  width = 375,
}: LibraryScreenProps) {
  const compact = width < 350;
  const mounted = useRef(true);
  const generation = useRef(0);
  const [records, setRecords] = useState<LibraryRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [confirmingClear, setConfirmingClear] = useState(false);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      generation.current += 1;
    };
  }, []);

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null;
    if (focused) timer = setTimeout(() => void hydrate(), 0);
    else generation.current += 1;
    // Loading on each focus makes a just-saved Research Run appear without background subscriptions.
    return () => {
      if (timer) clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focused, store]);

  async function hydrate() {
    const request = ++generation.current;
    setLoading(true);
    setError(null);
    try {
      const snapshot = await store.list();
      if (!mounted.current || request !== generation.current) return;
      setRecords([...snapshot.records]);
      setNotice(snapshot.corruptedCount > 0
        ? `Removed ${snapshot.corruptedCount} unreadable saved record${snapshot.corruptedCount === 1 ? '' : 's'}.`
        : null);
    } catch (loadError) {
      if (!mounted.current || request !== generation.current) return;
      setError(message(loadError));
    } finally {
      if (mounted.current && request === generation.current) setLoading(false);
    }
  }

  function open(record: LibraryRecord) {
    void haptics.selectionAsync().catch(() => undefined);
    router.push({ pathname: '/research', params: { symbol: record.symbol, period: record.period, recordId: record.id } });
  }

  async function remove(record: LibraryRecord) {
    try {
      await store.delete(record.id);
      if (!mounted.current) return;
      setRecords((current) => current.filter((item) => item.id !== record.id));
      setNotice(`${record.symbol} research deleted from this device.`);
      void haptics.notificationAsync(Haptics.NotificationFeedbackType.Success).catch(() => undefined);
    } catch (deleteError) {
      if (mounted.current) setError(message(deleteError));
    }
  }

  async function clearAll() {
    try {
      await store.clear();
      if (!mounted.current) return;
      setRecords([]);
      setConfirmingClear(false);
      setNotice('Library cleared on this device.');
      void haptics.notificationAsync(Haptics.NotificationFeedbackType.Success).catch(() => undefined);
    } catch (clearError) {
      if (mounted.current) setError(message(clearError));
    }
  }

  function openDefaultLens() {
    void haptics.selectionAsync().catch(() => undefined);
    router.push({ pathname: '/ticker/[symbol]', params: { symbol: 'AAPL' } });
  }

  return (
    <SafeAreaView edges={['top', 'left', 'right']} style={styles.safeArea}>
      <ScrollView contentContainerStyle={[styles.content, compact && styles.compactContent]} contentInsetAdjustmentBehavior="automatic" style={styles.scroll} testID="library-content">
        <Text style={styles.eyebrow}>ON THIS DEVICE</Text>
        <View style={styles.headingRow}>
          <View style={styles.headingCopy}>
            <Text accessibilityRole="header" style={styles.title}>Library</Text>
            <Text style={styles.intro}>Completed research, kept offline with bounded local storage.</Text>
          </View>
          {records.length ? <Text style={styles.count}>{records.length} / 24</Text> : null}
        </View>

        {notice ? <Text accessibilityLiveRegion="polite" style={styles.notice}>{notice}</Text> : null}
        {error ? (
          <View accessibilityRole="alert" accessible style={styles.errorCard}>
            <Text style={styles.errorText}>{error}</Text>
            <Pressable accessibilityLabel="Retry Library" accessibilityRole="button" onPress={() => void hydrate()} style={({ pressed }) => [styles.secondaryAction, pressed && styles.pressed]}>
              <Text style={styles.secondaryText}>Retry</Text>
            </Pressable>
          </View>
        ) : null}
        {loading ? <Text accessibilityLiveRegion="polite" style={styles.loading}>Opening the on-device Library…</Text> : null}

        {!loading && !error && records.length === 0 ? (
          <View accessibilityLabel="Library is empty" style={styles.emptyState}>
            <View style={styles.archiveMark}>
              <View style={styles.archiveLine} />
              <Ionicons color={colors.cyan} name="archive-outline" size={30} />
              <View style={styles.archiveLine} />
            </View>
            <Text style={styles.emptyLabel}>EMPTY ARCHIVE</Text>
            <Text style={styles.emptyTitle}>Nothing saved yet</Text>
            <Text style={styles.emptyBody}>Finish a Research Run, then choose Save on this device. Cancelled and incomplete work never appears here.</Text>
            <Pressable accessibilityLabel="Open AAPL Ticker Lens" accessibilityRole="button" onPress={openDefaultLens} style={({ pressed }) => [styles.primaryAction, pressed && styles.pressed]}>
              <Text style={styles.primaryText}>Explore AAPL</Text>
            </Pressable>
          </View>
        ) : null}

        {records.length ? (
          <View style={styles.records}>
            {records.map((record) => (
              <View accessibilityLabel={`Saved ${record.symbol} research`} key={record.id} style={styles.recordCard}>
                <View style={styles.recordHeading}>
                  <View style={styles.recordIdentity}>
                    <Text style={styles.symbol}>{record.symbol}</Text>
                    <Text style={styles.period}>{record.period}</Text>
                  </View>
                  <Text style={styles.deviceLabel}>On this device</Text>
                </View>
                <Text style={styles.summary}>{record.summary || 'No written summary was returned.'}</Text>
                <Text style={styles.provenance}>{record.source.transport === 'stream' ? 'Research stream' : 'Non-streaming fallback'} · {new Date(record.generatedAt).toLocaleString()}</Text>
                <View style={styles.recordActions}>
                  <Pressable accessibilityLabel={`Open saved ${record.symbol} research`} accessibilityRole="button" onPress={() => open(record)} style={({ pressed }) => [styles.openAction, pressed && styles.pressed]}>
                    <Text style={styles.openText}>Open research</Text>
                  </Pressable>
                  <Pressable accessibilityLabel={`Delete saved ${record.symbol} research`} accessibilityRole="button" onPress={() => void remove(record)} style={({ pressed }) => [styles.deleteAction, pressed && styles.pressed]}>
                    <Text style={styles.deleteText}>Delete</Text>
                  </Pressable>
                </View>
              </View>
            ))}
          </View>
        ) : null}

        {records.length ? (
          <Pressable accessibilityLabel="Clear all saved research" accessibilityRole="button" onPress={() => setConfirmingClear(true)} style={({ pressed }) => [styles.clearAction, pressed && styles.pressed]}>
            <Text style={styles.clearText}>Clear All</Text>
          </Pressable>
        ) : null}

        {confirmingClear ? (
          <View accessibilityRole="alert" accessible style={styles.confirmCard}>
            <Text style={styles.confirmTitle}>Clear this Library?</Text>
            <Text style={styles.confirmBody}>Every saved Research Run on this device will be removed. This cannot be undone.</Text>
            <View style={styles.confirmActions}>
              <Pressable accessibilityLabel="Cancel clearing Library" accessibilityRole="button" onPress={() => setConfirmingClear(false)} style={({ pressed }) => [styles.secondaryAction, pressed && styles.pressed]}>
                <Text style={styles.secondaryText}>Cancel</Text>
              </Pressable>
              <Pressable accessibilityLabel="Confirm clear all saved research" accessibilityRole="button" onPress={() => void clearAll()} style={({ pressed }) => [styles.dangerAction, pressed && styles.pressed]}>
                <Text style={styles.dangerText}>Clear All</Text>
              </Pressable>
            </View>
          </View>
        ) : null}
      </ScrollView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safeArea: { backgroundColor: colors.graphite, flex: 1 },
  scroll: { flex: 1 },
  content: { alignSelf: 'center', gap: spacing.md, maxWidth: layout.maximumContentWidth, padding: spacing.lg, paddingBottom: spacing.xxxl, width: '100%' },
  compactContent: { paddingHorizontal: spacing.md },
  eyebrow: { ...typography.eyebrow, color: colors.cyan },
  headingRow: { alignItems: 'flex-start', flexDirection: 'row', flexWrap: 'wrap', gap: spacing.md, justifyContent: 'space-between' },
  headingCopy: { flexGrow: 1, flexShrink: 1, minWidth: 210 },
  title: { ...typography.display, color: colors.ink },
  intro: { ...typography.body, color: colors.inkSecondary, marginTop: spacing.xs },
  count: { ...typography.micro, backgroundColor: colors.mineralSoft, borderRadius: radii.pill, color: colors.mint, minHeight: layout.minimumTouchTarget, paddingHorizontal: spacing.sm, paddingVertical: spacing.sm },
  notice: { ...typography.caption, color: colors.cyan },
  loading: { ...typography.body, color: colors.inkSecondary },
  errorCard: { borderColor: colors.coral, borderRadius: radii.lg, borderWidth: 1, gap: spacing.sm, padding: spacing.md },
  errorText: { ...typography.body, color: colors.coral },
  emptyState: { alignItems: 'flex-start', backgroundColor: colors.graphiteRaised, borderColor: colors.mineral, borderRadius: radii.xl, borderWidth: 1, gap: spacing.sm, padding: spacing.lg },
  archiveMark: { alignItems: 'center', flexDirection: 'row', gap: spacing.sm, marginBottom: spacing.sm, width: '100%' },
  archiveLine: { backgroundColor: colors.mineral, flex: 1, height: 1 },
  emptyLabel: { ...typography.eyebrow, color: colors.inkMuted },
  emptyTitle: { ...typography.title, color: colors.ink },
  emptyBody: { ...typography.body, color: colors.inkSecondary },
  records: { gap: spacing.md },
  recordCard: { backgroundColor: colors.graphiteRaised, borderColor: colors.mineral, borderRadius: radii.xl, borderWidth: 1, gap: spacing.sm, padding: spacing.md },
  recordHeading: { alignItems: 'flex-start', flexDirection: 'row', flexWrap: 'wrap', gap: spacing.sm, justifyContent: 'space-between' },
  recordIdentity: { alignItems: 'baseline', flexDirection: 'row', flexWrap: 'wrap', gap: spacing.sm },
  symbol: { ...typography.headline, color: colors.mint },
  period: { ...typography.caption, color: colors.inkMuted },
  deviceLabel: { ...typography.micro, color: colors.cyan },
  summary: { ...typography.body, color: colors.ink },
  provenance: { ...typography.caption, color: colors.inkMuted },
  recordActions: { flexDirection: 'row', flexWrap: 'wrap', gap: spacing.sm },
  openAction: { alignItems: 'center', backgroundColor: colors.mint, borderRadius: radii.md, flexGrow: 1, justifyContent: 'center', minHeight: layout.minimumTouchTarget, minWidth: 150, paddingHorizontal: spacing.md, paddingVertical: spacing.sm },
  openText: { ...typography.label, color: colors.graphite },
  deleteAction: { alignItems: 'center', borderColor: colors.coral, borderRadius: radii.md, justifyContent: 'center', minHeight: layout.minimumTouchTarget, minWidth: 88, paddingHorizontal: spacing.md, paddingVertical: spacing.sm },
  deleteText: { ...typography.label, color: colors.coral },
  clearAction: { alignItems: 'center', borderColor: colors.coral, borderRadius: radii.md, borderWidth: 1, justifyContent: 'center', minHeight: layout.minimumTouchTarget, paddingHorizontal: spacing.md, paddingVertical: spacing.sm },
  clearText: { ...typography.label, color: colors.coral },
  confirmCard: { backgroundColor: colors.graphiteRaised, borderColor: colors.coral, borderRadius: radii.xl, borderWidth: 1, gap: spacing.sm, padding: spacing.md },
  confirmTitle: { ...typography.title, color: colors.ink },
  confirmBody: { ...typography.body, color: colors.inkSecondary },
  confirmActions: { flexDirection: 'row', flexWrap: 'wrap', gap: spacing.sm },
  primaryAction: { alignItems: 'center', alignSelf: 'stretch', backgroundColor: colors.mint, borderRadius: radii.md, justifyContent: 'center', minHeight: layout.minimumTouchTarget, paddingHorizontal: spacing.md, paddingVertical: spacing.sm },
  primaryText: { ...typography.label, color: colors.graphite },
  secondaryAction: { alignItems: 'center', borderColor: colors.mineral, borderRadius: radii.md, borderWidth: 1, flexGrow: 1, justifyContent: 'center', minHeight: layout.minimumTouchTarget, minWidth: 120, paddingHorizontal: spacing.md, paddingVertical: spacing.sm },
  secondaryText: { ...typography.label, color: colors.ink },
  dangerAction: { alignItems: 'center', backgroundColor: colors.coral, borderRadius: radii.md, flexGrow: 1, justifyContent: 'center', minHeight: layout.minimumTouchTarget, minWidth: 120, paddingHorizontal: spacing.md, paddingVertical: spacing.sm },
  dangerText: { ...typography.label, color: colors.graphite },
  pressed: { opacity: 0.72 },
});
