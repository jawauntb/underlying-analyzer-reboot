import { useEffect, useRef, useState } from 'react';
import { Pressable, StyleSheet, Text, View } from 'react-native';

import { RequestCoordinator, type ApiClient } from '@/src/api/client';
import type { AlertItem, AlertRow, WatchlistAlertsResponse } from '@/src/api/contracts';
import { API_ENDPOINTS } from '@/src/api/endpoints';
import AsyncState from '@/src/components/ui/AsyncState';
import { AsyncCache, TTL_MS, type CacheRecord, type CacheRequestDescriptor } from '@/src/state/cache';
import type { NetworkReachability } from '@/src/state/network';
import { colors, layout, radii, spacing, typography } from '@/src/theme/tokens';

const defaultCache = new AsyncCache();

export type LensOverviewClient = Pick<ApiClient, 'baseUrl' | 'watchlistAlerts'>;
export type LensOverviewCache = Pick<AsyncCache, 'read' | 'write'>;

type OverviewData = {
  response: WatchlistAlertsResponse;
  row: AlertRow;
  alerts: AlertItem[];
};

type OverviewState =
  | { status: 'loading' | 'empty-offline'; data: null; fetchedAt: null; error?: string }
  | { status: 'fresh' | 'stale-refreshing' | 'offline-stale' | 'partial'; data: OverviewData; fetchedAt: number; error?: string }
  | { status: 'error'; data: OverviewData | null; fetchedAt: number | null; error: string };

type LensOverviewProps = {
  cache?: LensOverviewCache;
  client: LensOverviewClient;
  now?: () => number;
  reachability?: NetworkReachability;
  symbol: string;
};

function cacheDescriptor(client: LensOverviewClient, symbol: string): CacheRequestDescriptor {
  return {
    baseUrl: client.baseUrl,
    method: 'POST',
    route: API_ENDPOINTS.alerts,
    body: { ticker: symbol },
  };
}

function overviewData(response: WatchlistAlertsResponse, symbol: string): OverviewData | null {
  const row = response.rows.find((candidate) => candidate.ticker === symbol);
  if (!row) return null;
  return {
    response,
    row,
    alerts: response.alerts.filter((alert) => alert.ticker === symbol),
  };
}

function errorMessage(error: unknown): string {
  return error instanceof Error && error.message ? error.message : 'The security overview could not be loaded.';
}

function missingMessage(response: WatchlistAlertsResponse, symbol: string): string {
  const reported = response.errors.find((error) => error.ticker === symbol)?.error;
  if (reported) return reported;
  return response.rows.length
    ? `Overview response did not match ${symbol}.`
    : `No overview is available for ${symbol}.`;
}

function textValue(record: Record<string, unknown>, ...keys: string[]): string | null {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === 'string' && value.trim()) return value.trim();
  }
  return null;
}

function numberValue(record: Record<string, unknown>, ...keys: string[]): number | null {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === 'number' && Number.isFinite(value)) return value;
  }
  return null;
}

function booleanValue(record: Record<string, unknown>, ...keys: string[]): boolean | null {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === 'boolean') return value;
  }
  return null;
}

function formatPrice(value: number | null): string {
  return value === null ? 'Price unavailable' : `$${value.toFixed(2)}`;
}

function formatChange(value: number | null): string {
  if (value === null) return 'Change unavailable';
  return `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`;
}

function formatPercent(value: number): string {
  return `${value >= 0 ? '+' : ''}${(value * 100).toFixed(1)}%`;
}

function optionalNumber(value: number | null, digits = 1): string {
  return value === null ? 'Unavailable' : value.toFixed(digits);
}

function optionalPercent(value: number | null): string {
  return value === null ? 'Unavailable' : formatPercent(value);
}

function titleCase(value: string): string {
  return value.replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function sentenceCase(value: string): string {
  return value ? `${value.charAt(0).toUpperCase()}${value.slice(1).toLowerCase()}` : value;
}

function freshness(value: number): string {
  return `Updated ${new Date(value).toLocaleString()}`;
}

function source(data: OverviewData): string {
  return data.row.provider ?? data.response.provider ?? 'Source not reported';
}

function priceRegime(row: AlertRow): { value: string; detail: string } {
  const recommendation = textValue(row.ridge, 'recommendation', 'state') ?? 'Unavailable';
  const state = textValue(row.ridge, 'state');
  const trendConfirmed = booleanValue(row.ridge, 'trend_confirmed', 'trendConfirmed');
  const totalReturn = numberValue(row.ridge, 'total_return', 'totalReturn');
  const details = [
    state && state.toUpperCase() !== recommendation.toUpperCase() ? titleCase(state) : null,
    trendConfirmed === null ? null : trendConfirmed ? 'Trend confirmed' : 'Trend not confirmed',
    totalReturn === null ? null : `${formatPercent(totalReturn)} strategy return`,
  ].filter(Boolean);
  return { value: recommendation.toUpperCase(), detail: details.join(' · ') || 'No additional Ridge evidence reported.' };
}

function participation(row: AlertRow): { value: string; detail: string } {
  const state = textValue(row.flow, 'state', 'signal') ?? 'Unavailable';
  const signal = textValue(row.flow, 'signal');
  const score = numberValue(row.flow, 'score');
  const freshLong = booleanValue(row.flow, 'fresh_long', 'freshLong');
  const freshShort = booleanValue(row.flow, 'fresh_short', 'freshShort');
  const volume = numberValue(row.flow, 'volume_score', 'volumeScore');
  const shift = freshLong ? 'Fresh long shift' : freshShort ? 'Fresh short shift' : null;
  const detail = [
    signal && signal.toUpperCase() !== state.toUpperCase() ? titleCase(signal) : null,
    shift,
    volume === null ? null : `Volume score ${volume.toFixed(1)}`,
  ].filter(Boolean).join(' · ');
  return {
    value: `${state.toUpperCase()}${score === null ? '' : ` · ${score.toFixed(1)}`}`,
    detail: detail || 'No additional Flow evidence reported.',
  };
}

function valueLocation(row: AlertRow): { value: string; detail: string } {
  const location = textValue(row.auction, 'location') ?? 'Unavailable';
  const poc = numberValue(row.auction, 'poc');
  const vah = numberValue(row.auction, 'vah');
  const val = numberValue(row.auction, 'val');
  const distance = numberValue(row.auction, 'distance_to_poc', 'distanceToPoc');
  const detail = [
    poc === null ? null : `POC $${poc.toFixed(2)}`,
    val === null || vah === null ? null : `Value $${val.toFixed(2)}–$${vah.toFixed(2)}`,
    distance === null ? null : `${formatPercent(distance)} from POC`,
  ].filter(Boolean).join(' · ');
  return { value: sentenceCase(location), detail: detail || 'No additional auction evidence reported.' };
}

function EvidenceCard({ label, value, detail }: { label: string; value: string; detail: string }) {
  return (
    <View style={styles.evidenceCard}>
      <Text style={styles.cardLabel}>{label}</Text>
      <Text style={styles.evidenceValue}>{value}</Text>
      <Text style={styles.cardDetail}>{detail}</Text>
    </View>
  );
}

function OverviewContent({ data, fetchedAt }: { data: OverviewData; fetchedAt: number }) {
  const { row, alerts, response } = data;
  const priceEvidence = priceRegime(row);
  const participationEvidence = participation(row);
  const valueEvidence = valueLocation(row);
  const fundamentals = row.fundamentals;
  const providerNote = row.providerNote ?? response.providerNote;

  return (
    <View style={styles.overviewContent}>
      <View style={styles.identityCard}>
        <View style={styles.identityCopy}>
          <Text style={styles.cardLabel}>SECURITY</Text>
          <Text style={styles.company}>{row.name ?? row.ticker}</Text>
          <Text style={styles.classification}>{[row.sector, row.industry].filter(Boolean).join(' · ') || 'Classification unavailable'}</Text>
        </View>
        <View style={styles.marketCopy}>
          <Text style={styles.price}>{formatPrice(row.price)}</Text>
          <Text style={[styles.change, (row.changePercent ?? 0) < 0 && styles.negative]}>{formatChange(row.changePercent)}</Text>
        </View>
      </View>

      <View style={styles.setupCard}>
        <View style={styles.setupTopline}>
          <Text style={styles.cardLabel}>CURRENT SETUP</Text>
          <Text style={styles.lane}>{row.lane ?? 'Review'}{row.score === null ? '' : ` · ${row.score.toFixed(1)}`}</Text>
        </View>
        <Text style={styles.setup}>{row.setup ?? 'No setup description is available.'}</Text>
      </View>

      <View style={styles.section}>
        <Text accessibilityRole="header" style={styles.sectionTitle}>Current alerts</Text>
        {alerts.length ? alerts.map((alert) => (
          <View key={alert.id} style={styles.alertCard}>
            <View style={styles.alertTopline}>
              <Text style={styles.alertTitle}>{alert.title}</Text>
              <Text style={styles.alertSeverity}>{alert.severity} · {alert.category}</Text>
            </View>
            <Text style={styles.cardDetail}>{alert.message}</Text>
            <Text style={styles.alertAction}>{alert.action}</Text>
          </View>
        )) : <Text style={styles.cardDetail}>No current alert rules fired for this security.</Text>}
      </View>

      <View style={styles.section}>
        <Text accessibilityRole="header" style={styles.sectionTitle}>Why this setup</Text>
        <View style={styles.evidenceGrid}>
          <EvidenceCard label="PRICE REGIME" {...priceEvidence} />
          <EvidenceCard label="PARTICIPATION" {...participationEvidence} />
          <EvidenceCard label="VALUE LOCATION" {...valueEvidence} />
        </View>
      </View>

      <View style={styles.businessCard}>
        <Text style={styles.cardLabel}>BUSINESS</Text>
        <Text style={styles.businessSummary}>{fundamentals.businessSummary ?? 'Business summary unavailable.'}</Text>
        <View style={styles.fundamentalGrid}>
          <EvidenceCard label="MARKET CAP" value={fundamentals.marketCap ?? 'Unavailable'} detail={fundamentals.country ?? 'Country unavailable'} />
          <EvidenceCard label="TRAILING P/E" value={optionalNumber(fundamentals.trailingPe)} detail="Reported earnings multiple" />
          <EvidenceCard label="FORWARD P/E" value={optionalNumber(fundamentals.forwardPe)} detail="Estimated earnings multiple" />
          <EvidenceCard label="REVENUE GROWTH" value={optionalPercent(fundamentals.revenueGrowth)} detail="Year-over-year" />
          <EvidenceCard label="PROFIT MARGIN" value={optionalPercent(fundamentals.profitMargins)} detail="Net income margin" />
          <EvidenceCard
            label="52-WEEK RANGE"
            value={fundamentals.fiftyTwoWeekLow === null || fundamentals.fiftyTwoWeekHigh === null
              ? 'Unavailable'
              : `$${fundamentals.fiftyTwoWeekLow.toFixed(2)}–$${fundamentals.fiftyTwoWeekHigh.toFixed(2)}`}
            detail="Reported low to high"
          />
          <EvidenceCard
            label="ANALYST TARGET"
            value={fundamentals.targetMeanPrice === null ? 'Unavailable' : `$${fundamentals.targetMeanPrice.toFixed(2)}`}
            detail={fundamentals.analystCount === null ? 'Analyst count unavailable' : `${fundamentals.analystCount} analysts · ${fundamentals.recommendation ? titleCase(fundamentals.recommendation) : 'View unavailable'}`}
          />
        </View>
      </View>

      <View style={styles.provenanceCard}>
        <Text style={styles.provenance}>{source(data)} · {freshness(fetchedAt)}</Text>
        {providerNote ? <Text style={styles.cardDetail}>{providerNote}</Text> : null}
      </View>
    </View>
  );
}

export default function LensOverview({
  cache = defaultCache,
  client,
  now = Date.now,
  reachability = 'unknown',
  symbol,
}: LensOverviewProps) {
  const coordinator = useRef(new RequestCoordinator<WatchlistAlertsResponse>());
  const generation = useRef(0);
  const [state, setState] = useState<OverviewState>({ status: 'loading', data: null, fetchedAt: null });

  useEffect(() => {
    const activeCoordinator = coordinator.current;
    void bootstrap();
    return () => {
      generation.current += 1;
      activeCoordinator.cancel();
    };
    // The symbol defines one overview lifecycle; reachability changes only affect retry availability.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbol]);

  async function bootstrap() {
    const descriptor = cacheDescriptor(client, symbol);
    const currentGeneration = ++generation.current;
    let cached: CacheRecord<WatchlistAlertsResponse> | null = null;
    try {
      cached = await cache.read<WatchlistAlertsResponse>(descriptor);
    } catch {
      cached = null;
    }
    if (currentGeneration !== generation.current) return;
    const cachedData = cached ? overviewData(cached.data, symbol) : null;

    if (reachability === 'offline') {
      setState(cached && cachedData
        ? { status: 'offline-stale', data: cachedData, fetchedAt: cached.fetchedAt }
        : { status: 'empty-offline', data: null, fetchedAt: null });
      return;
    }
    if (cached && cachedData && now() - cached.fetchedAt <= TTL_MS.pulse) {
      setState({ status: 'fresh', data: cachedData, fetchedAt: cached.fetchedAt });
      return;
    }
    setState(cached && cachedData
      ? { status: 'stale-refreshing', data: cachedData, fetchedAt: cached.fetchedAt }
      : { status: 'loading', data: null, fetchedAt: null });
    await requestLive(cachedData, cached?.fetchedAt ?? null, currentGeneration);
  }

  async function requestLive(
    priorData: OverviewData | null,
    priorFetchedAt: number | null,
    currentGeneration = ++generation.current,
  ) {
    try {
      const result = await coordinator.current.run((signal) => client.watchlistAlerts({ ticker: symbol }, { signal }));
      if (!result.accepted || currentGeneration !== generation.current) return;
      const fetchedAt = now();
      const data = overviewData(result.value, symbol);
      if (!data) {
        setState({ status: 'error', data: null, fetchedAt: null, error: missingMessage(result.value, symbol) });
        return;
      }
      setState({
        status: result.value.status === 'partial' ? 'partial' : 'fresh',
        data,
        fetchedAt,
      });
      void cache.write(cacheDescriptor(client, symbol), result.value, fetchedAt).catch(() => undefined);
    } catch (error) {
      if (currentGeneration !== generation.current) return;
      setState({ status: 'error', data: priorData, fetchedAt: priorFetchedAt, error: errorMessage(error) });
    }
  }

  function retry() {
    if (reachability === 'offline') return;
    const priorData = state.data;
    const priorFetchedAt = state.fetchedAt;
    setState(priorData && priorFetchedAt !== null
      ? { status: 'stale-refreshing', data: priorData, fetchedAt: priorFetchedAt }
      : { status: 'loading', data: null, fetchedAt: null });
    void requestLive(priorData, priorFetchedAt);
  }

  const retryDisabled = reachability === 'offline';

  return (
    <View style={styles.container}>
      <View style={styles.headingRow}>
        <View style={styles.headingCopy}>
          <Text style={styles.eyebrow}>SECURITY OVERVIEW</Text>
          <Text accessibilityRole="header" style={styles.heading}>What matters now</Text>
        </View>
        {state.status !== 'loading' ? (
          <Pressable
            accessibilityLabel="Refresh overview"
            accessibilityRole="button"
            accessibilityState={{ disabled: retryDisabled }}
            disabled={retryDisabled}
            onPress={retry}
            style={({ pressed }) => [styles.retry, retryDisabled && styles.disabled, pressed && styles.pressed]}>
            <Text style={styles.retryText}>REFRESH</Text>
          </Pressable>
        ) : null}
      </View>

      {state.status === 'loading' ? <AsyncState title="Loading security overview" message="Fetching one lightweight alert snapshot." /> : null}
      {state.status === 'stale-refreshing' ? <AsyncState title="Saved overview · refreshing" message={`Showing saved evidence ${freshness(state.fetchedAt)} while one update is in flight.`} tone="warning" /> : null}
      {state.status === 'offline-stale' ? <AsyncState actionDisabled actionLabel="Retry overview" message={`Saved evidence remains visible ${freshness(state.fetchedAt)}. Reconnect to update it.`} onAction={retry} title="Offline · saved overview" tone="warning" /> : null}
      {state.status === 'empty-offline' ? <AsyncState actionDisabled actionLabel="Retry overview" message="Reconnect to load this security for the first time." onAction={retry} title="No saved overview offline" tone="warning" /> : null}
      {state.status === 'error' ? <AsyncState actionDisabled={retryDisabled} actionLabel="Retry overview" message={state.error} onAction={retry} title="Security overview unavailable" tone="error" /> : null}
      {state.data && state.fetchedAt !== null ? <OverviewContent data={state.data} fetchedAt={state.fetchedAt} /> : null}
      {state.status === 'partial' && state.data.response.errors.length ? (
        <AsyncState message={state.data.response.errors.map((error) => `${error.ticker}: ${error.error}`).join('\n')} title="Some overview data is partial" tone="warning" />
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  container: { gap: spacing.md },
  headingRow: { alignItems: 'center', flexDirection: 'row', flexWrap: 'wrap', gap: spacing.md, justifyContent: 'space-between' },
  headingCopy: { flexGrow: 1, flexShrink: 1, minWidth: 210 },
  eyebrow: { ...typography.eyebrow, color: colors.cyan },
  heading: { ...typography.headline, color: colors.ink, marginTop: spacing.xs },
  retry: { alignItems: 'center', borderColor: colors.mineral, borderRadius: radii.md, borderWidth: 1, justifyContent: 'center', minHeight: layout.minimumTouchTarget, paddingHorizontal: spacing.md },
  retryText: { ...typography.micro, color: colors.mint },
  disabled: { opacity: 0.45 },
  pressed: { opacity: 0.72 },
  overviewContent: { gap: spacing.md },
  identityCard: { alignItems: 'flex-start', backgroundColor: colors.graphiteRaised, borderColor: colors.mineral, borderRadius: radii.xl, borderWidth: 1, flexDirection: 'row', flexWrap: 'wrap', gap: spacing.md, justifyContent: 'space-between', padding: spacing.md },
  identityCopy: { flexGrow: 1, flexShrink: 1, gap: spacing.xs, minWidth: 190 },
  marketCopy: { alignItems: 'flex-end', gap: spacing.xs },
  cardLabel: { ...typography.micro, color: colors.inkMuted },
  company: { ...typography.headline, color: colors.ink },
  classification: { ...typography.caption, color: colors.inkSecondary },
  price: { ...typography.title, color: colors.ink },
  change: { ...typography.label, color: colors.mint },
  negative: { color: colors.coral },
  setupCard: { backgroundColor: colors.mineralSoft, borderRadius: radii.lg, gap: spacing.sm, padding: spacing.md },
  setupTopline: { alignItems: 'center', flexDirection: 'row', flexWrap: 'wrap', gap: spacing.sm, justifyContent: 'space-between' },
  lane: { ...typography.caption, color: colors.cyan },
  setup: { ...typography.body, color: colors.ink },
  section: { gap: spacing.sm },
  sectionTitle: { ...typography.title, color: colors.ink },
  alertCard: { borderColor: colors.mineral, borderRadius: radii.lg, borderWidth: 1, gap: spacing.xs, padding: spacing.md },
  alertTopline: { alignItems: 'flex-start', flexDirection: 'row', flexWrap: 'wrap', gap: spacing.sm, justifyContent: 'space-between' },
  alertTitle: { ...typography.label, color: colors.ink },
  alertSeverity: { ...typography.micro, color: colors.coral },
  alertAction: { ...typography.caption, color: colors.mint },
  evidenceGrid: { flexDirection: 'row', flexWrap: 'wrap', gap: spacing.sm },
  evidenceCard: { backgroundColor: colors.graphiteRaised, borderRadius: radii.lg, flexGrow: 1, flexShrink: 1, gap: spacing.xs, minWidth: 180, padding: spacing.md },
  evidenceValue: { ...typography.label, color: colors.ink },
  cardDetail: { ...typography.caption, color: colors.inkSecondary },
  businessCard: { borderTopColor: colors.mineral, borderTopWidth: 1, gap: spacing.sm, paddingTop: spacing.md },
  businessSummary: { ...typography.body, color: colors.inkSecondary },
  fundamentalGrid: { flexDirection: 'row', flexWrap: 'wrap', gap: spacing.sm },
  provenanceCard: { gap: spacing.xs },
  provenance: { ...typography.caption, color: colors.mint },
});
