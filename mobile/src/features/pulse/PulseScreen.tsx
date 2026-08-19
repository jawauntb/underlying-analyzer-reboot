import { useIsFocused } from '@react-navigation/native';
import { useRouter, type Href } from 'expo-router';
import { useEffect, useRef, useState } from 'react';
import { Pressable, ScrollView, StyleSheet, Text, useWindowDimensions, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { ApiClient, RequestCoordinator } from '@/src/api/client';
import type { WatchlistAlertsRequest, WatchlistAlertsResponse } from '@/src/api/contracts';
import { API_ENDPOINTS } from '@/src/api/endpoints';
import AsyncState from '@/src/components/ui/AsyncState';
import { newestSavedList, type SavedListsContextValue, useSavedLists } from '@/src/features/lists/watchlists';
import { AsyncCache, TTL_MS, type CacheRecord, type CacheRequestDescriptor } from '@/src/state/cache';
import { type NetworkReachability, useNetworkReachability } from '@/src/state/network';
import { colors, layout, radii, spacing, typography } from '@/src/theme/tokens';

import PulseCard from './PulseCard';
import PulseDigestCard from './PulseDigestCard';

const DEFAULT_SYMBOLS = ['AAPL', 'MSFT', 'NVDA'] as const;
const defaultClient = new ApiClient();
const defaultCache = new AsyncCache();

type PulseClient = Pick<ApiClient, 'baseUrl' | 'watchlistAlerts'>;
type PulseCache = Pick<AsyncCache, 'read' | 'write'>;
type PulseRouter = { push(href: Href): void };

type PulseViewState =
  | { status: 'waiting' | 'loading' | 'empty-offline'; data: null; fetchedAt: null; error?: string }
  | { status: 'fresh' | 'stale-refreshing' | 'offline-stale' | 'partial'; data: WatchlistAlertsResponse; fetchedAt: number; error?: string }
  | { status: 'empty-online'; data: WatchlistAlertsResponse; fetchedAt: number; error?: string }
  | { status: 'error'; data: WatchlistAlertsResponse | null; fetchedAt: number | null; error: string };

export type PulseScreenProps = {
  client?: PulseClient;
  cache?: PulseCache;
  reachability?: NetworkReachability;
  listsState?: Pick<SavedListsContextValue, 'hydrated' | 'lists'>;
  focused?: boolean;
  router?: PulseRouter;
  now?: () => number;
};

function message(error: unknown): string {
  return error instanceof Error && error.message ? error.message : 'The market pulse could not be loaded.';
}

function cacheDescriptor(client: PulseClient, request: WatchlistAlertsRequest): CacheRequestDescriptor {
  return {
    baseUrl: client.baseUrl,
    method: 'POST',
    route: API_ENDPOINTS.alerts,
    body: { tickers: request.tickers },
  };
}

function freshnessLabel(fetchedAt: number | null): string {
  return fetchedAt === null ? 'time unavailable' : `as of ${new Date(fetchedAt).toLocaleString()}`;
}

function ConnectedPulseScreen(props: PulseScreenProps) {
  const reachability = useNetworkReachability();
  const listsState = useSavedLists();
  const focused = useIsFocused();
  const router = useRouter();
  return (
    <PulseController
      {...props}
      focused={focused}
      listsState={listsState}
      reachability={reachability}
      router={router}
    />
  );
}

export default function PulseScreen(props: PulseScreenProps) {
  const injected =
    props.reachability !== undefined &&
    props.listsState !== undefined &&
    props.focused !== undefined &&
    props.router !== undefined;
  return injected ? <PulseController {...props as Required<Pick<PulseScreenProps, 'reachability' | 'listsState' | 'focused' | 'router'>>} client={props.client} cache={props.cache} now={props.now} /> : <ConnectedPulseScreen {...props} />;
}

function PulseController({
  client = defaultClient,
  cache = defaultCache,
  reachability = 'unknown',
  listsState = { hydrated: false, lists: [] },
  focused = false,
  router = { push: () => undefined },
  now = Date.now,
}: PulseScreenProps) {
  const { width } = useWindowDimensions();
  const compact = width < 350;
  const coordinatorRef = useRef(new RequestCoordinator<WatchlistAlertsResponse>());
  const requestGeneration = useRef(0);
  const wasFocused = useRef(false);
  const symbolSnapshot = useRef<readonly string[]>(DEFAULT_SYMBOLS);
  const sourceLabel = useRef('Default focus list');
  const [focusEpoch, setFocusEpoch] = useState(0);
  const [bootstrapComplete, setBootstrapComplete] = useState(false);
  const [state, setState] = useState<PulseViewState>({ status: 'waiting', data: null, fetchedAt: null });

  useEffect(() => {
    if (!focused) {
      wasFocused.current = false;
      requestGeneration.current += 1;
      coordinatorRef.current.cancel();
      return;
    }
    if (listsState.hydrated && !wasFocused.current) {
      wasFocused.current = true;
      setFocusEpoch((value) => value + 1);
    }
  }, [focused, listsState.hydrated]);

  useEffect(() => {
    if (!focusEpoch) return;
    const newest = newestSavedList(listsState.lists);
    symbolSnapshot.current = newest?.symbols.length ? [...newest.symbols] : DEFAULT_SYMBOLS;
    sourceLabel.current = newest?.name ?? 'Default focus list';
    setBootstrapComplete(false);
    void bootstrap(symbolSnapshot.current);
    // A focus epoch deliberately snapshots lists and reachability; reconnecting or saving while focused does not retry.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusEpoch]);

  useEffect(() => () => coordinatorRef.current.cancel(), []);

  useEffect(() => {
    if (reachability !== 'offline' || !bootstrapComplete) return;
    requestGeneration.current += 1;
    coordinatorRef.current.cancel();
    setState((current) =>
      current.data
        ? { status: 'offline-stale', data: current.data, fetchedAt: current.fetchedAt ?? now() }
        : { status: 'empty-offline', data: null, fetchedAt: null },
    );
  }, [bootstrapComplete, now, reachability]);

  async function bootstrap(symbols: readonly string[]) {
    const request = { tickers: [...symbols] };
    const descriptor = cacheDescriptor(client, request);
    const generation = ++requestGeneration.current;
    let cached: CacheRecord<WatchlistAlertsResponse> | null = null;
    try {
      cached = await cache.read<WatchlistAlertsResponse>(descriptor);
    } catch {
      cached = null;
    }
    if (generation !== requestGeneration.current) return;

    if (reachability === 'offline') {
      setState(
        cached
          ? { status: 'offline-stale', data: cached.data, fetchedAt: cached.fetchedAt }
          : { status: 'empty-offline', data: null, fetchedAt: null },
      );
      setBootstrapComplete(true);
      return;
    }

    if (cached && now() - cached.fetchedAt <= TTL_MS.pulse) {
      setState({ status: 'fresh', data: cached.data, fetchedAt: cached.fetchedAt });
      setBootstrapComplete(true);
      return;
    }
    setState(
      cached
        ? { status: 'stale-refreshing', data: cached.data, fetchedAt: cached.fetchedAt }
        : { status: 'loading', data: null, fetchedAt: null },
    );
    await requestLive(request, cached?.data ?? null, cached?.fetchedAt ?? null, generation, true);
  }

  async function requestLive(
    request: WatchlistAlertsRequest,
    priorData: WatchlistAlertsResponse | null,
    priorFetchedAt: number | null,
    generation = ++requestGeneration.current,
    bootstrap = false,
  ) {
    try {
      const result = await coordinatorRef.current.run((signal) => client.watchlistAlerts(request, { signal }));
      if (!result.accepted || generation !== requestGeneration.current) return;
      const fetchedAt = now();
      const data = result.value;
      setState(
        data.rows.length === 0
          ? { status: 'empty-online', data, fetchedAt }
          : data.status === 'partial'
            ? { status: 'partial', data, fetchedAt }
            : { status: 'fresh', data, fetchedAt },
      );
      if (data.status === 'fresh' || data.status === 'partial') {
        void cache.write(cacheDescriptor(client, request), data, fetchedAt).catch(() => undefined);
      }
    } catch (error) {
      if (generation !== requestGeneration.current) return;
      setState({ status: 'error', data: priorData, fetchedAt: priorFetchedAt, error: message(error) });
    } finally {
      if (bootstrap && generation === requestGeneration.current) setBootstrapComplete(true);
    }
  }

  function refresh() {
    if (!bootstrapComplete || reachability === 'offline') return;
    const prior = state.data;
    const fetchedAt = state.fetchedAt;
    setState(
      prior
        ? { status: 'stale-refreshing', data: prior, fetchedAt: fetchedAt ?? now() }
        : { status: 'loading', data: null, fetchedAt: null },
    );
    void requestLive({ tickers: [...symbolSnapshot.current] }, prior, fetchedAt);
  }

  const rows = state.data?.rows ?? [];
  const label = freshnessLabel(state.fetchedAt);
  const retryDisabled = reachability === 'offline' || !bootstrapComplete;

  return (
    <SafeAreaView edges={['top', 'left', 'right']} style={styles.safeArea}>
      <ScrollView contentContainerStyle={[styles.content, compact && styles.compactContent]} contentInsetAdjustmentBehavior="automatic">
        <View style={styles.masthead}>
          <View style={styles.headingCopy}>
            <Text style={styles.eyebrow}>UNDERCURRENT / SIGNAL DESK</Text>
            <Text accessibilityRole="header" style={styles.title}>Pulse</Text>
            <Text style={styles.intro}>Ranked setups from one lightweight alert digest.</Text>
          </View>
          {bootstrapComplete ? (
            <Pressable
              accessibilityLabel="Refresh Pulse"
              accessibilityRole="button"
              accessibilityState={{ disabled: retryDisabled }}
              disabled={retryDisabled}
              onPress={refresh}
              style={({ pressed }) => [styles.refresh, retryDisabled && styles.disabled, pressed && styles.pressed]}>
              <Text style={styles.refreshText}>REFRESH</Text>
            </Pressable>
          ) : null}
        </View>

        {!listsState.hydrated || state.status === 'waiting' ? (
          <AsyncState title="Preparing your Pulse" message="Loading your saved lists on this device." />
        ) : null}
        {state.status === 'loading' ? (
          <AsyncState accessibilityLabel="Loading market pulse" title="Loading market Pulse" message="Fetching one alert digest for this list." />
        ) : null}
        {state.status === 'stale-refreshing' ? (
          <AsyncState title="Cached Pulse · refreshing" message={`Showing saved rows ${label} while one update is in flight.`} tone="warning" />
        ) : null}
        {state.status === 'offline-stale' ? (
          <AsyncState
            title="Offline · stale Pulse"
            message={`Saved rows remain visible ${label}. Reconnect, then use Retry.`}
            actionLabel="Retry Pulse"
            actionDisabled
            onAction={refresh}
            tone="warning"
          />
        ) : null}
        {state.status === 'empty-offline' ? (
          <AsyncState
            title="No cached Pulse offline"
            message="Reconnect to load this list for the first time."
            actionLabel="Retry Pulse"
            actionDisabled
            onAction={refresh}
            tone="warning"
          />
        ) : null}
        {state.status === 'empty-online' ? (
          <AsyncState title="No setups matched" message="The alert digest returned no ranked rows. Try again when the market changes." actionLabel="Refresh Pulse" onAction={refresh} />
        ) : null}
        {state.status === 'error' ? (
          <AsyncState title="Pulse unavailable" message={state.error} actionLabel="Retry Pulse" actionDisabled={retryDisabled} onAction={refresh} tone="error" />
        ) : null}
        {state.status === 'partial' && state.data.errors.length ? (
          <AsyncState title="Some symbols need attention" message={state.data.errors.map((error) => `${error.ticker}: ${error.error}`).join('\n')} tone="warning" />
        ) : null}

        {state.data && rows.length ? (
          <PulseDigestCard
            digest={state.data.digest}
            freshness={label}
            sourceLabel={sourceLabel.current}
          />
        ) : null}

        {rows.length ? (
          <View style={styles.rows}>
            {rows.map((row, index) => (
              <PulseCard
                fallbackRank={index + 1}
                freshness={label}
                key={row.ticker}
                onPress={() => router.push({ pathname: '/ticker/[symbol]', params: { symbol: row.ticker } })}
                provider={state.data?.provider ?? null}
                row={row}
              />
            ))}
          </View>
        ) : null}
      </ScrollView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safeArea: { flex: 1, backgroundColor: colors.graphite },
  content: {
    alignSelf: 'center',
    gap: spacing.md,
    maxWidth: layout.maximumContentWidth,
    paddingBottom: spacing.xxxl,
    paddingHorizontal: spacing.lg,
    paddingTop: spacing.md,
    width: '100%',
  },
  compactContent: { paddingHorizontal: spacing.md },
  masthead: { alignItems: 'flex-start', flexDirection: 'row', flexWrap: 'wrap', gap: spacing.md, justifyContent: 'space-between' },
  headingCopy: { flexGrow: 1, flexShrink: 1, minWidth: 210 },
  eyebrow: { ...typography.eyebrow, color: colors.cyan },
  title: { ...typography.display, color: colors.ink, marginTop: spacing.xs },
  intro: { ...typography.body, color: colors.inkSecondary, marginTop: spacing.xs },
  refresh: {
    alignItems: 'center',
    borderColor: colors.mineral,
    borderRadius: radii.md,
    borderWidth: 1,
    justifyContent: 'center',
    minHeight: layout.minimumTouchTarget,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.sm,
  },
  refreshText: { ...typography.micro, color: colors.mint },
  rows: { gap: spacing.md },
  disabled: { opacity: 0.45 },
  pressed: { opacity: 0.72 },
});
