import Ionicons from '@expo/vector-icons/Ionicons';
import { useRouter, type Href } from 'expo-router';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  useWindowDimensions,
  View,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { ApiClient, RequestCoordinator } from '@/src/api/client';
import type { SecuritySearchResponse, SecuritySearchResult } from '@/src/api/contracts';
import AsyncState from '@/src/components/ui/AsyncState';
import {
  newestSavedList,
  type SavedListsContextValue,
  useSavedLists,
} from '@/src/features/lists/watchlists';
import { colors, layout, radii, spacing, typography } from '@/src/theme/tokens';

import {
  RecentSearchStore,
  type RecentSearchRecord,
  type RecentSearchStoreApi,
} from './recent-searches';

const DEFAULT_SYMBOLS = ['AAPL', 'MSFT', 'NVDA'] as const;
const SEARCH_DEBOUNCE_MS = 250;
const defaultClient = new ApiClient();
const defaultRecentStore = new RecentSearchStore();

type SearchClient = Pick<ApiClient, 'searchSecurities'>;
type SearchRouter = { push(href: Href): void };
type SearchState =
  | { status: 'idle'; query: '' }
  | { status: 'loading'; query: string }
  | { status: 'results'; query: string; response: SecuritySearchResponse }
  | { status: 'empty'; query: string; provider: string }
  | { status: 'error'; query: string; message: string };

export type SearchScreenProps = {
  client?: SearchClient;
  recentStore?: RecentSearchStoreApi;
  listsState?: Pick<SavedListsContextValue, 'hydrated' | 'lists'>;
  router?: SearchRouter;
  debounceMs?: number;
};

function message(error: unknown): string {
  return error instanceof Error && error.message ? error.message : 'Security search could not be completed.';
}

function assetTypeLabel(assetType: SecuritySearchResult['assetType']): string {
  return assetType.replace('_', ' ').toUpperCase();
}

function ConnectedSearchScreen(props: SearchScreenProps) {
  const listsState = useSavedLists();
  const router = useRouter();
  return <SearchController {...props} listsState={listsState} router={router} />;
}

export default function SearchScreen(props: SearchScreenProps) {
  return props.listsState && props.router
    ? <SearchController {...props} />
    : <ConnectedSearchScreen {...props} />;
}

function SearchController({
  client = defaultClient,
  recentStore = defaultRecentStore,
  listsState = { hydrated: false, lists: [] },
  router = { push: () => undefined },
  debounceMs = SEARCH_DEBOUNCE_MS,
}: SearchScreenProps) {
  const { width } = useWindowDimensions();
  const compact = width < 350;
  const coordinator = useRef(new RequestCoordinator<SecuritySearchResponse>());
  const generation = useRef(0);
  const debounceTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [query, setQuery] = useState('');
  const [state, setState] = useState<SearchState>({ status: 'idle', query: '' });
  const [recents, setRecents] = useState<readonly RecentSearchRecord[]>([]);

  const quickAccess = useMemo(() => {
    const newest = newestSavedList(listsState.lists);
    const source = newest?.symbols.length ? newest : null;
    const symbols = source ? source.symbols : DEFAULT_SYMBOLS;
    return {
      label: source?.name ?? 'Market starters',
      symbols: [...new Set(symbols)],
    };
  }, [listsState.lists]);

  const cancelPending = useCallback(() => {
    if (debounceTimer.current) {
      clearTimeout(debounceTimer.current);
      debounceTimer.current = null;
    }
    generation.current += 1;
    coordinator.current.cancel();
  }, []);

  const runSearch = useCallback(async (rawQuery: string) => {
    const trimmed = rawQuery.trim();
    if (!trimmed) return;
    const requestGeneration = ++generation.current;
    setState({ status: 'loading', query: trimmed });
    try {
      const result = await coordinator.current.run((signal) =>
        client.searchSecurities({ query: trimmed, limit: 10 }, { signal }),
      );
      if (!result.accepted || requestGeneration !== generation.current) return;
      setState(result.value.results.length
        ? { status: 'results', query: trimmed, response: result.value }
        : { status: 'empty', query: trimmed, provider: result.value.provider });
    } catch (error) {
      if (requestGeneration !== generation.current) return;
      setState({ status: 'error', query: trimmed, message: message(error) });
    }
  }, [client]);

  useEffect(() => {
    let mounted = true;
    void recentStore.hydrate()
      .then((records) => {
        if (mounted) setRecents(records);
      })
      .catch(() => {
        if (mounted) setRecents([]);
      });
    return () => { mounted = false; };
  }, [recentStore]);

  useEffect(() => {
    cancelPending();
    const trimmed = query.trim();
    if (trimmed.length < 2) {
      setState({ status: 'idle', query: '' });
      return;
    }
    debounceTimer.current = setTimeout(() => {
      debounceTimer.current = null;
      void runSearch(trimmed);
    }, debounceMs);
    return () => {
      if (debounceTimer.current) clearTimeout(debounceTimer.current);
    };
  }, [cancelPending, debounceMs, query, runSearch]);

  useEffect(() => () => cancelPending(), [cancelPending]);

  function submit() {
    const trimmed = query.trim();
    if (!trimmed) return;
    cancelPending();
    void runSearch(trimmed);
  }

  function openResult(result: SecuritySearchResult) {
    router.push({ pathname: '/ticker/[symbol]', params: { symbol: result.symbol } });
    void recentStore.record(result).then(setRecents).catch(() => undefined);
  }

  function openQuickSymbol(symbol: string) {
    router.push({ pathname: '/ticker/[symbol]', params: { symbol } });
  }

  const showDiscovery = query.trim().length === 0;

  return (
    <SafeAreaView edges={['top', 'left', 'right']} style={styles.safeArea}>
      <ScrollView
        contentContainerStyle={[styles.content, compact && styles.compactContent]}
        contentInsetAdjustmentBehavior="automatic"
        keyboardShouldPersistTaps="handled">
        <View style={styles.masthead}>
          <Text style={styles.eyebrow}>UNDERCURRENT / SECURITY FINDER</Text>
          <Text accessibilityRole="header" style={styles.title}>Search</Text>
          <Text style={styles.intro}>Find a company or ticker, then open its Lens without running analysis.</Text>
        </View>

        <View style={styles.searchRow}>
          <View style={styles.inputShell}>
            <Ionicons color={colors.inkMuted} name="search" size={20} />
            <TextInput
              accessibilityLabel="Search companies and tickers"
              autoCapitalize="characters"
              autoCorrect={false}
              enterKeyHint="search"
              onChangeText={setQuery}
              onSubmitEditing={submit}
              placeholder="Apple or AAPL"
              placeholderTextColor={colors.inkMuted}
              returnKeyType="search"
              style={styles.input}
              value={query}
            />
            {query ? (
              <Pressable
                accessibilityLabel="Clear search"
                accessibilityRole="button"
                onPress={() => setQuery('')}
                style={({ pressed }) => [styles.clearAction, pressed && styles.pressed]}>
                <Ionicons color={colors.inkSecondary} name="close-circle" size={22} />
              </Pressable>
            ) : null}
          </View>
          <Pressable
            accessibilityLabel="Submit search"
            accessibilityRole="button"
            accessibilityState={{ disabled: !query.trim() }}
            disabled={!query.trim()}
            onPress={submit}
            style={({ pressed }) => [styles.searchAction, !query.trim() && styles.disabled, pressed && styles.pressed]}>
            <Text style={styles.searchActionText}>Search</Text>
          </Pressable>
        </View>

        {query.trim().length === 1 && state.status === 'idle' ? (
          <Text style={styles.hint}>Press Search to look up a one-character ticker.</Text>
        ) : null}

        {showDiscovery ? (
          <View style={styles.discovery}>
            {recents.length ? (
              <View style={styles.section}>
                <Text style={styles.sectionEyebrow}>RECENT</Text>
                <Text style={styles.sectionTitle}>Back to a Lens</Text>
                <View style={styles.resultList}>
                  {recents.map((recent) => (
                    <ResultRow
                      accessibilityLabel={`Open recent ${recent.symbol} ${recent.name || recent.symbol} Lens`}
                      key={recent.symbol}
                      onPress={() => openResult(recent)}
                      result={recent}
                    />
                  ))}
                </View>
              </View>
            ) : null}

            <View style={styles.section}>
              <Text style={styles.sectionEyebrow}>QUICK ACCESS</Text>
              <Text style={styles.sectionTitle}>{quickAccess.label}</Text>
              {!listsState.hydrated ? <Text style={styles.hint}>Using market starters while saved lists load.</Text> : null}
              <View style={styles.quickGrid}>
                {quickAccess.symbols.map((symbol) => (
                  <Pressable
                    accessibilityLabel={`Open ${symbol} Lens from ${quickAccess.label}`}
                    accessibilityRole="button"
                    key={symbol}
                    onPress={() => openQuickSymbol(symbol)}
                    style={({ pressed }) => [styles.quickAction, pressed && styles.pressed]}>
                    <Text style={styles.quickSymbol}>{symbol}</Text>
                    <Ionicons color={colors.cyan} name="arrow-forward" size={18} />
                  </Pressable>
                ))}
              </View>
            </View>
          </View>
        ) : null}

        {state.status === 'loading' ? (
          <AsyncState
            accessibilityLabel="Loading security search"
            message={`Looking for “${state.query}” by symbol and company name.`}
            title="Searching securities"
          />
        ) : null}
        {state.status === 'error' ? (
          <View accessible accessibilityRole="alert">
            <AsyncState
              actionLabel="Retry search"
              message={state.message}
              onAction={() => void runSearch(state.query)}
              title="Search unavailable"
              tone="error"
            />
          </View>
        ) : null}
        {state.status === 'empty' ? (
          <AsyncState
            message={`No supported securities were returned by ${state.provider}. Try a company name or exact ticker.`}
            title={`No matches for “${state.query}”`}
          />
        ) : null}
        {state.status === 'results' ? (
          <View style={styles.resultsSection}>
            <View style={styles.resultsHeading}>
              <Text style={styles.sectionTitle}>Matches</Text>
              <Text style={styles.provider}>Results from {state.response.provider}</Text>
            </View>
            <View style={styles.resultList}>
              {state.response.results.map((result) => (
                <ResultRow
                  accessibilityLabel={`Open ${result.symbol} ${result.name || result.symbol} Lens`}
                  key={`${result.symbol}-${result.exchange}`}
                  onPress={() => openResult(result)}
                  result={result}
                />
              ))}
            </View>
          </View>
        ) : null}
      </ScrollView>
    </SafeAreaView>
  );
}

function ResultRow({
  accessibilityLabel,
  onPress,
  result,
}: {
  accessibilityLabel: string;
  onPress(): void;
  result: SecuritySearchResult;
}) {
  const displayName = result.name || result.symbol;
  const metadata = [result.exchange, assetTypeLabel(result.assetType)].filter(Boolean).join(' · ');
  return (
    <Pressable
      accessibilityLabel={accessibilityLabel}
      accessibilityRole="button"
      onPress={onPress}
      style={({ pressed }) => [styles.result, pressed && styles.pressed]}>
      <View style={styles.symbolBadge}>
        <Text adjustsFontSizeToFit={false} style={styles.symbol}>{result.symbol}</Text>
      </View>
      <View style={styles.resultCopy}>
        <Text style={styles.resultName}>{displayName}</Text>
        <Text style={styles.resultMeta}>{metadata}</Text>
      </View>
      <Ionicons color={colors.cyan} name="chevron-forward" size={20} />
    </Pressable>
  );
}

const styles = StyleSheet.create({
  safeArea: { flex: 1, backgroundColor: colors.graphite },
  content: { alignSelf: 'center', gap: spacing.lg, maxWidth: layout.maximumContentWidth, paddingBottom: spacing.xxxl, paddingHorizontal: spacing.lg, paddingTop: spacing.md, width: '100%' },
  compactContent: { paddingHorizontal: spacing.md },
  masthead: { gap: spacing.xs },
  eyebrow: { ...typography.eyebrow, color: colors.cyan },
  title: { ...typography.display, color: colors.ink },
  intro: { ...typography.body, color: colors.inkSecondary },
  searchRow: { alignItems: 'stretch', flexDirection: 'row', flexWrap: 'wrap', gap: spacing.sm },
  inputShell: { alignItems: 'center', backgroundColor: colors.graphiteRaised, borderColor: colors.mineral, borderRadius: radii.md, borderWidth: 1, flex: 1, flexDirection: 'row', minHeight: layout.minimumTouchTarget, minWidth: 220, paddingLeft: spacing.md },
  input: { ...typography.body, color: colors.ink, flex: 1, minHeight: layout.minimumTouchTarget, paddingHorizontal: spacing.sm, paddingVertical: spacing.sm },
  clearAction: { alignItems: 'center', justifyContent: 'center', minHeight: layout.minimumTouchTarget, minWidth: layout.minimumTouchTarget },
  searchAction: { alignItems: 'center', backgroundColor: colors.mint, borderRadius: radii.md, justifyContent: 'center', minHeight: layout.minimumTouchTarget, paddingHorizontal: spacing.md, paddingVertical: spacing.sm },
  searchActionText: { ...typography.label, color: colors.graphite },
  discovery: { gap: spacing.lg },
  section: { backgroundColor: colors.graphiteRaised, borderColor: colors.mineral, borderRadius: radii.xl, borderWidth: 1, gap: spacing.sm, padding: spacing.md },
  sectionEyebrow: { ...typography.eyebrow, color: colors.cyan },
  sectionTitle: { ...typography.title, color: colors.ink },
  quickGrid: { flexDirection: 'row', flexWrap: 'wrap', gap: spacing.sm },
  quickAction: { alignItems: 'center', backgroundColor: colors.graphite, borderColor: colors.mineral, borderRadius: radii.pill, borderWidth: 1, flexDirection: 'row', gap: spacing.sm, minHeight: layout.minimumTouchTarget, paddingHorizontal: spacing.md, paddingVertical: spacing.sm },
  quickSymbol: { ...typography.label, color: colors.ink },
  hint: { ...typography.caption, color: colors.inkMuted },
  resultsSection: { gap: spacing.sm },
  resultsHeading: { gap: spacing.xs },
  provider: { ...typography.caption, color: colors.inkMuted },
  resultList: { gap: spacing.sm },
  result: { alignItems: 'center', backgroundColor: colors.graphiteRaised, borderColor: colors.mineral, borderRadius: radii.lg, borderWidth: 1, flexDirection: 'row', gap: spacing.md, minHeight: layout.minimumTouchTarget, padding: spacing.md },
  symbolBadge: { alignItems: 'center', backgroundColor: colors.graphite, borderRadius: radii.md, justifyContent: 'center', minHeight: layout.minimumTouchTarget, minWidth: 72, paddingHorizontal: spacing.sm, paddingVertical: spacing.xs },
  symbol: { ...typography.label, color: colors.mint, flexShrink: 1 },
  resultCopy: { flex: 1, gap: spacing.xs, minWidth: 0 },
  resultName: { ...typography.label, color: colors.ink, flexShrink: 1 },
  resultMeta: { ...typography.caption, color: colors.inkMuted, flexShrink: 1 },
  disabled: { opacity: 0.45 },
  pressed: { opacity: 0.72 },
});
