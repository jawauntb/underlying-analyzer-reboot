import { useEffect, useRef, useState } from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';

import { RequestCoordinator, type ApiClient } from '@/src/api/client';
import type { AuctionResponse, ChartDataset } from '@/src/api/contracts';
import { API_ENDPOINTS } from '@/src/api/endpoints';
import { isRecord } from '@/src/api/guards';
import { AuctionChart } from '@/src/components/charts/AuctionChart';
import AsyncState from '@/src/components/ui/AsyncState';
import { AsyncCache, TTL_MS, type CacheRequestDescriptor } from '@/src/state/cache';
import type { NetworkReachability } from '@/src/state/network';
import { colors, layout, radii, spacing, typography } from '@/src/theme/tokens';

import { LENS_AUCTION_PERIODS } from './lens-model';
import { errorMessage, explicitProvider, formatTimestamp } from './lens-utils';

const defaultCache = new AsyncCache();

const PERIODS = [
  { label: '1M', value: LENS_AUCTION_PERIODS[1], spoken: '1 month' },
  { label: '3M', value: LENS_AUCTION_PERIODS[2], spoken: '3 months' },
  { label: '6M', value: LENS_AUCTION_PERIODS[3], spoken: '6 months' },
  { label: '1Y', value: LENS_AUCTION_PERIODS[4], spoken: '1 year' },
] as const;

type Period = (typeof PERIODS)[number]['value'];
type ChartData = { dataset: ChartDataset; response: AuctionResponse };
type ChartClient = Pick<ApiClient, 'auction' | 'baseUrl'>;
type ChartCache = Pick<AsyncCache, 'read' | 'write'>;

type ChartState =
  | { status: 'loading' | 'empty-offline'; data: null; fetchedAt: null; message?: string }
  | { status: 'fresh' | 'refreshing' | 'offline'; data: ChartData; fetchedAt: number; message?: string }
  | { status: 'error'; data: ChartData | null; fetchedAt: number | null; message: string };

type PriceValuePanelProps = {
  cache?: ChartCache;
  client: ChartClient;
  fontScale: number;
  now?: () => number;
  reachability: NetworkReachability;
  symbol: string;
  width: number;
};

function descriptor(client: ChartClient, symbol: string, period: Period): CacheRequestDescriptor {
  return {
    baseUrl: client.baseUrl,
    method: 'POST',
    route: API_ENDPOINTS.auction,
    body: { ticker: symbol, period },
  };
}

function chartData(response: AuctionResponse, symbol: string): ChartData | null {
  const dataset = response.datasets.find((candidate) => candidate.ticker === symbol);
  return dataset ? { dataset, response } : null;
}

function responseError(response: AuctionResponse, symbol: string): string {
  return response.errors.find((entry) => entry.ticker === symbol)?.error
    ?? (response.datasets.length ? `Price response did not match ${symbol}.` : `No price history is available for ${symbol}.`);
}

function source(data: ChartData): string {
  const rawProvider = isRecord(data.dataset.raw) ? data.dataset.raw.provider : null;
  return explicitProvider(data.response.provider, rawProvider, data.dataset.meta.provider);
}

function compactResponse(response: AuctionResponse): AuctionResponse {
  return {
    ...response,
    datasets: response.datasets.map((dataset) => ({
      ...dataset,
      raw: { provider: isRecord(dataset.raw) ? dataset.raw.provider : undefined },
      rows: [],
      series: { ohlcv: dataset.series.ohlcv },
    })),
  };
}

export default function PriceValuePanel({
  cache = defaultCache,
  client,
  fontScale,
  now = Date.now,
  reachability,
  symbol,
  width,
}: PriceValuePanelProps) {
  const [period, setPeriod] = useState<Period>('3mo');
  const [state, setState] = useState<ChartState>({ status: 'loading', data: null, fetchedAt: null });
  const coordinator = useRef(new RequestCoordinator<AuctionResponse>());
  const generation = useRef(0);
  const selected = PERIODS.find((candidate) => candidate.value === period) ?? PERIODS[1];
  const title = `${symbol} ${selected.label} Price & value`;

  useEffect(() => {
    const activeCoordinator = coordinator.current;
    const currentGeneration = ++generation.current;
    void bootstrap(currentGeneration, period);
    return () => {
      generation.current += 1;
      activeCoordinator.cancel();
    };
    // The selected symbol, period, and network truth define one chart lifecycle.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [period, reachability, symbol]);

  async function bootstrap(currentGeneration: number, requestedPeriod: Period) {
    setState({ status: 'loading', data: null, fetchedAt: null });
    let cached = null;
    let cacheReadFailed = false;
    try {
      cached = await cache.read<AuctionResponse>(descriptor(client, symbol, requestedPeriod));
    } catch {
      cacheReadFailed = true;
    }
    if (currentGeneration !== generation.current) return;
    const cachedData = cached ? chartData(cached.data, symbol) : null;

    if (reachability === 'offline') {
      setState(cached && cachedData
        ? { status: 'offline', data: cachedData, fetchedAt: cached.fetchedAt }
        : {
          status: 'empty-offline',
          data: null,
          fetchedAt: null,
          message: cacheReadFailed
            ? 'Saved chart storage could not be read. Reconnect to load this chart.'
            : 'Reconnect to load this chart for the first time.',
        });
      return;
    }
    if (cached && cachedData && now() - cached.fetchedAt <= TTL_MS.charts) {
      setState({ status: 'fresh', data: cachedData, fetchedAt: cached.fetchedAt });
      return;
    }
    setState(cached && cachedData
      ? { status: 'refreshing', data: cachedData, fetchedAt: cached.fetchedAt }
      : { status: 'loading', data: null, fetchedAt: null });
    await requestLive({
      requestedPeriod,
      currentGeneration,
      priorData: cachedData,
      priorFetchedAt: cached?.fetchedAt ?? null,
    });
  }

  async function requestLive({
    requestedPeriod,
    currentGeneration,
    priorData,
    priorFetchedAt,
  }: {
    requestedPeriod: Period;
    currentGeneration: number;
    priorData: ChartData | null;
    priorFetchedAt: number | null;
  }) {
    try {
      const result = await coordinator.current.run((signal) =>
        client.auction({ ticker: symbol, period: requestedPeriod }, { signal }),
      );
      if (!result.accepted || currentGeneration !== generation.current || requestedPeriod !== period) return;
      const data = chartData(result.value, symbol);
      if (!data) {
        setState({ status: 'error', data: priorData, fetchedAt: priorFetchedAt, message: responseError(result.value, symbol) });
        return;
      }
      const fetchedAt = now();
      const notice = result.value.errors.map((entry) => `${entry.ticker}: ${entry.error}`).join('\n') || undefined;
      setState({ status: 'fresh', data, fetchedAt, message: notice });
      void cache.write(descriptor(client, symbol, requestedPeriod), compactResponse(result.value), fetchedAt).catch(() => {
        if (currentGeneration !== generation.current || requestedPeriod !== period) return;
        setState((current) => {
          if (!current.data || current.data.response !== result.value) return current;
          const warning = 'Chart loaded, but it could not be saved for offline use.';
          return { ...current, message: current.message ? `${current.message}\n${warning}` : warning };
        });
      });
    } catch (error) {
      if (currentGeneration !== generation.current || requestedPeriod !== period) return;
      setState({ status: 'error', data: priorData, fetchedAt: priorFetchedAt, message: errorMessage(error, 'Price history could not be loaded.') });
    }
  }

  function choosePeriod(next: Period) {
    if (next === period) return;
    generation.current += 1;
    setState({ status: 'loading', data: null, fetchedAt: null });
    setPeriod(next);
  }

  function retry() {
    if (reachability === 'offline') return;
    const currentGeneration = ++generation.current;
    const priorData = state.data;
    const priorFetchedAt = state.fetchedAt;
    setState(priorData && priorFetchedAt !== null
      ? { status: 'refreshing', data: priorData, fetchedAt: priorFetchedAt }
      : { status: 'loading', data: null, fetchedAt: null });
    void requestLive({ requestedPeriod: period, currentGeneration, priorData, priorFetchedAt });
  }

  return (
    <View style={styles.section}>
      <View style={styles.headingRow}>
        <View style={styles.headingCopy}>
          <Text style={styles.eyebrow}>MARKET PICTURE</Text>
          <Text accessibilityRole="header" style={styles.heading}>Price & value</Text>
          <Text style={styles.description}>Daily candles with auction value levels.</Text>
        </View>
        <View accessibilityRole="tablist" style={styles.periods}>
          {PERIODS.map((candidate) => {
            const active = candidate.value === period;
            return (
              <Pressable
                accessibilityLabel={`Show ${candidate.spoken} chart`}
                accessibilityRole="tab"
                accessibilityState={{ selected: active }}
                key={candidate.value}
                onPress={() => choosePeriod(candidate.value)}
                style={({ pressed }) => [styles.period, active && styles.periodActive, pressed && styles.pressed]}>
                <Text style={[styles.periodText, active && styles.periodTextActive]}>{candidate.label}</Text>
              </Pressable>
            );
          })}
        </View>
      </View>

      <View style={styles.chartFrame}>
        {state.status === 'loading' ? <AsyncState title="Loading price chart" message={`Fetching ${selected.spoken} of daily price and value data.`} /> : null}
        {state.status === 'refreshing' ? <AsyncState title="Saved chart · refreshing" message={`Showing saved data ${formatTimestamp(state.fetchedAt)} while this range updates.`} tone="warning" /> : null}
        {state.status === 'offline' ? <AsyncState actionDisabled actionLabel="Retry price chart" message={`Saved chart remains visible ${formatTimestamp(state.fetchedAt)}.`} onAction={retry} title="Offline · saved chart" tone="warning" /> : null}
        {state.status === 'empty-offline' ? <AsyncState actionDisabled actionLabel="Retry price chart" message={state.message ?? 'Reconnect to load this chart for the first time.'} onAction={retry} title="No saved chart offline" tone="warning" /> : null}
        {state.status === 'error' ? <AsyncState actionLabel="Retry price chart" message={state.message} onAction={retry} title="Price chart unavailable" tone="error" /> : null}
        {state.data && state.fetchedAt !== null ? (
          <View style={styles.chartContent}>
            <Text style={styles.provenance}>{source(state.data)} · {formatTimestamp(state.fetchedAt)}</Text>
            {state.data.response.providerNote ? <Text style={styles.note}>{state.data.response.providerNote}</Text> : null}
            {state.message && state.status !== 'error' ? <Text accessibilityRole="alert" style={styles.warning}>{state.message}</Text> : null}
            <AuctionChart dataset={state.data.dataset} fontScale={fontScale} title={title} width={width} />
          </View>
        ) : null}
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  section: {
    backgroundColor: colors.graphiteRaised,
    borderColor: colors.mineral,
    borderRadius: radii.xl,
    borderWidth: 1,
    gap: spacing.md,
    padding: spacing.md,
  },
  headingRow: { alignItems: 'flex-start', flexDirection: 'row', flexWrap: 'wrap', gap: spacing.md, justifyContent: 'space-between' },
  headingCopy: { flexGrow: 1, flexShrink: 1, gap: spacing.xs, minWidth: 190 },
  eyebrow: { ...typography.eyebrow, color: colors.cyan },
  heading: { ...typography.headline, color: colors.ink },
  description: { ...typography.caption, color: colors.inkSecondary },
  periods: { flexDirection: 'row', flexGrow: 1, flexWrap: 'wrap', gap: spacing.xs, justifyContent: 'flex-end' },
  period: {
    alignItems: 'center',
    borderColor: colors.mineral,
    borderRadius: radii.pill,
    borderWidth: 1,
    justifyContent: 'center',
    minHeight: layout.minimumTouchTarget,
    minWidth: layout.minimumTouchTarget,
    paddingHorizontal: spacing.sm,
  },
  periodActive: { backgroundColor: colors.mint, borderColor: colors.mint },
  periodText: { ...typography.micro, color: colors.inkSecondary },
  periodTextActive: { color: colors.graphite },
  chartFrame: { minHeight: 260 },
  chartContent: { gap: spacing.sm },
  provenance: { ...typography.caption, color: colors.mint },
  note: { ...typography.caption, color: colors.inkSecondary },
  warning: { ...typography.caption, color: colors.coral },
  pressed: { opacity: 0.72 },
});
