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

import ChartIntervalRail from './ChartIntervalRail';
import { CHART_INTERVAL_CHIPS, LENS_AUCTION_PERIODS, type ChartInterval } from './lens-model';
import { errorMessage, explicitProvider, formatFreshness, formatTimestamp, publicProviderNote } from './lens-utils';

const defaultCache = new AsyncCache();

const PERIODS = [
  { label: '1M', value: LENS_AUCTION_PERIODS[1], spoken: '1 month' },
  { label: '3M', value: LENS_AUCTION_PERIODS[2], spoken: '3 months' },
  { label: '6M', value: LENS_AUCTION_PERIODS[3], spoken: '6 months' },
  { label: '1Y', value: LENS_AUCTION_PERIODS[4], spoken: '1 year' },
] as const;

type Period = (typeof LENS_AUCTION_PERIODS)[number];
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
  /** Interval this panel opens on, from the reader's saved settings. */
  initialInterval?: ChartInterval;
  now?: () => number;
  reachability: NetworkReachability;
  symbol: string;
  width: number;
};

function descriptor(
  client: ChartClient,
  symbol: string,
  period: Period,
  interval: ChartInterval,
): CacheRequestDescriptor {
  return {
    baseUrl: client.baseUrl,
    method: 'POST',
    route: API_ENDPOINTS.auction,
    body: { ticker: symbol, period, interval },
  };
}

function intervalCopy(interval: ChartInterval): string {
  if (interval === '15m') return '15-minute candles with a live last bar.';
  if (interval === '1w') return 'Weekly candles with a live last bar.';
  return 'Daily candles with auction value levels and a live last bar.';
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
  initialInterval = '1d',
  now = Date.now,
  reachability,
  symbol,
  width,
}: PriceValuePanelProps) {
  const openingChip = CHART_INTERVAL_CHIPS.find((candidate) => candidate.value === initialInterval) ?? CHART_INTERVAL_CHIPS[1];
  const [period, setPeriod] = useState<Period>(openingChip.period);
  const [interval, setInterval] = useState<ChartInterval>(openingChip.value);
  const [state, setState] = useState<ChartState>({ status: 'loading', data: null, fetchedAt: null });
  const coordinator = useRef(new RequestCoordinator<AuctionResponse>());
  const generation = useRef(0);
  const selected = PERIODS.find((candidate) => candidate.value === period) ?? PERIODS[1];
  const selectedInterval = CHART_INTERVAL_CHIPS.find((candidate) => candidate.value === interval) ?? CHART_INTERVAL_CHIPS[1];
  const title = interval === '1d'
    ? `${symbol} ${selected.label} Price & value`
    : `${symbol} ${selectedInterval.label} Price & value`;

  useEffect(() => {
    const activeCoordinator = coordinator.current;
    const currentGeneration = ++generation.current;
    void bootstrap(currentGeneration, period, interval);
    return () => {
      generation.current += 1;
      activeCoordinator.cancel();
    };
    // The selected symbol, period, interval, and network truth define one chart lifecycle.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [interval, period, reachability, symbol]);

  async function bootstrap(currentGeneration: number, requestedPeriod: Period, requestedInterval: ChartInterval) {
    setState({ status: 'loading', data: null, fetchedAt: null });
    let cached = null;
    let cacheReadFailed = false;
    try {
      cached = await cache.read<AuctionResponse>(descriptor(client, symbol, requestedPeriod, requestedInterval));
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
      requestedInterval,
      currentGeneration,
      priorData: cachedData,
      priorFetchedAt: cached?.fetchedAt ?? null,
    });
  }

  async function requestLive({
    requestedPeriod,
    requestedInterval,
    currentGeneration,
    priorData,
    priorFetchedAt,
  }: {
    requestedPeriod: Period;
    requestedInterval: ChartInterval;
    currentGeneration: number;
    priorData: ChartData | null;
    priorFetchedAt: number | null;
  }) {
    try {
      const result = await coordinator.current.run((signal) =>
        client.auction({ ticker: symbol, period: requestedPeriod, interval: requestedInterval }, { signal }),
      );
      if (!result.accepted || currentGeneration !== generation.current || requestedPeriod !== period || requestedInterval !== interval) return;
      const data = chartData(result.value, symbol);
      if (!data) {
        setState({ status: 'error', data: priorData, fetchedAt: priorFetchedAt, message: responseError(result.value, symbol) });
        return;
      }
      const fetchedAt = now();
      const notice = result.value.errors.map((entry) => `${entry.ticker}: ${entry.error}`).join('\n') || undefined;
      setState({ status: 'fresh', data, fetchedAt, message: notice });
      void cache.write(descriptor(client, symbol, requestedPeriod, requestedInterval), compactResponse(result.value), fetchedAt).catch(() => {
        if (currentGeneration !== generation.current || requestedPeriod !== period || requestedInterval !== interval) return;
        setState((current) => {
          if (!current.data || current.data.response !== result.value) return current;
          const warning = 'Chart loaded, but it could not be saved for offline use.';
          return { ...current, message: current.message ? `${current.message}\n${warning}` : warning };
        });
      });
    } catch (error) {
      if (currentGeneration !== generation.current || requestedPeriod !== period || requestedInterval !== interval) return;
      setState({ status: 'error', data: priorData, fetchedAt: priorFetchedAt, message: errorMessage(error, 'Price history could not be loaded.') });
    }
  }

  function choosePeriod(next: Period) {
    if (next === period) return;
    generation.current += 1;
    setState({ status: 'loading', data: null, fetchedAt: null });
    setPeriod(next);
  }

  function chooseInterval(next: ChartInterval) {
    if (next === interval) return;
    const chip = CHART_INTERVAL_CHIPS.find((candidate) => candidate.value === next);
    generation.current += 1;
    setState({ status: 'loading', data: null, fetchedAt: null });
    setInterval(next);
    if (chip && next !== '1d') setPeriod(chip.period);
  }

  function retry() {
    if (reachability === 'offline') return;
    const currentGeneration = ++generation.current;
    const priorData = state.data;
    const priorFetchedAt = state.fetchedAt;
    setState(priorData && priorFetchedAt !== null
      ? { status: 'refreshing', data: priorData, fetchedAt: priorFetchedAt }
      : { status: 'loading', data: null, fetchedAt: null });
    void requestLive({ requestedPeriod: period, requestedInterval: interval, currentGeneration, priorData, priorFetchedAt });
  }

  return (
    <View style={styles.section}>
      <View style={styles.heading}>
        <Text style={styles.eyebrow}>MARKET PICTURE</Text>
        <Text accessibilityRole="header" style={styles.title}>Price & value</Text>
        <Text style={styles.description}>{intervalCopy(interval)}</Text>
      </View>

      <ChartIntervalRail interval={interval} onChange={chooseInterval} testID="price-value-interval-rail" />

      {interval === '1d' ? (
        <View accessibilityRole="tablist" style={styles.rangeRail} testID="price-value-range-rail">
          {PERIODS.map((candidate) => {
            const active = candidate.value === period;
            return (
              <Pressable
                accessibilityLabel={`Show ${candidate.spoken} chart`}
                accessibilityRole="tab"
                accessibilityState={{ selected: active }}
                key={candidate.value}
                onPress={() => choosePeriod(candidate.value)}
                style={({ pressed }) => [styles.range, active && styles.rangeActive, pressed && styles.pressed]}>
                <Text numberOfLines={1} style={[styles.rangeText, active && styles.rangeTextActive]}>
                  {candidate.label}
                </Text>
              </Pressable>
            );
          })}
        </View>
      ) : null}

      <View style={styles.chartFrame}>
        {state.status === 'loading' ? <AsyncState title="Loading price chart" message={`Fetching ${selectedInterval.spoken} price and value data.`} /> : null}
        {state.status === 'refreshing' ? <AsyncState title="Saved chart · refreshing" message={`Showing saved data ${formatTimestamp(state.fetchedAt)} while this range updates.`} tone="warning" /> : null}
        {state.status === 'offline' ? <AsyncState actionDisabled actionLabel="Retry price chart" message={`Saved chart remains visible ${formatTimestamp(state.fetchedAt)}.`} onAction={retry} title="Offline · saved chart" tone="warning" /> : null}
        {state.status === 'empty-offline' ? <AsyncState actionDisabled actionLabel="Retry price chart" message={state.message ?? 'Reconnect to load this chart for the first time.'} onAction={retry} title="No saved chart offline" tone="warning" /> : null}
        {state.status === 'error' ? <AsyncState actionLabel="Retry price chart" message={state.message} onAction={retry} title="Price chart unavailable" tone="error" /> : null}
        {state.data && state.fetchedAt !== null ? (
          <View style={styles.chartContent}>
            <Text style={styles.provenance}>{source(state.data)} · {formatFreshness(state.fetchedAt, now)}</Text>
            {publicProviderNote(state.data.response.providerNote)
              ? <Text style={styles.note}>{publicProviderNote(state.data.response.providerNote)}</Text>
              : null}
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
  heading: { gap: spacing.xs },
  eyebrow: { ...typography.eyebrow, color: colors.cyan },
  title: { ...typography.headline, color: colors.ink },
  description: { ...typography.caption, color: colors.inkSecondary },
  // The range rail is a single-line row sized by flex, so no wrapped chip can ever
  // be laid out over the chart meta line beneath it.
  rangeRail: { flexDirection: 'row', gap: spacing.xs },
  range: {
    alignItems: 'center',
    borderColor: colors.mineral,
    borderRadius: radii.pill,
    borderWidth: 1,
    flex: 1,
    justifyContent: 'center',
    minHeight: layout.minimumTouchTarget,
    paddingHorizontal: spacing.xs,
  },
  rangeActive: { backgroundColor: colors.graphiteSoft, borderColor: colors.mint },
  rangeText: { ...typography.micro, color: colors.inkMuted },
  rangeTextActive: { color: colors.mint },
  chartFrame: { minHeight: 260 },
  chartContent: { gap: spacing.sm },
  provenance: { ...typography.micro, color: colors.inkMuted },
  note: { ...typography.caption, color: colors.inkSecondary },
  warning: { ...typography.caption, color: colors.coral },
  pressed: { opacity: 0.72 },
});
