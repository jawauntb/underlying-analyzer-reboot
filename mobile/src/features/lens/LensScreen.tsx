import Ionicons from '@expo/vector-icons/Ionicons';
import * as Haptics from 'expo-haptics';
import { useRouter, type Href } from 'expo-router';
import { useEffect, useMemo, useRef, useState } from 'react';
import { Pressable, ScrollView, StyleSheet, Text, useWindowDimensions, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { ApiClient, RequestCoordinator } from '@/src/api/client';
import type { AuctionResponse, ChartDataset, MoneylineResponse, OptionsChainResponse, TorqueResponse } from '@/src/api/contracts';
import { isRecord } from '@/src/api/guards';
import { AuctionChart } from '@/src/components/charts/AuctionChart';
import { MoneylineChart } from '@/src/components/charts/MoneylineChart';
import { TorqueChart } from '@/src/components/charts/TorqueChart';
import AsyncState from '@/src/components/ui/AsyncState';
import MetricCard from '@/src/components/ui/MetricCard';
import type { NetworkReachability } from '@/src/state/network';
import { useNetworkReachability } from '@/src/state/network';
import { DEFAULT_PREFERENCES, usePreferences, type Preferences } from '@/src/state/preferences';
import { colors, layout, radii, spacing, typography } from '@/src/theme/tokens';

import ChartIntervalRail from './ChartIntervalRail';
import LensOverview, { type LensOverviewCache, type LensOverviewClient } from './LensOverview';
import PriceValuePanel from './PriceValuePanel';
import LiveQuoteCard from './LiveQuoteCard';
import MarketDataStatusCard from './MarketDataStatusCard';
import OptionsPulseCard from './OptionsPulseCard';
import {
  LENS_AUCTION_PERIODS,
  type ChartInterval,
  normalizeLensSymbol,
  RESEARCH_DEPTH_DESCRIPTIONS,
  RESEARCH_DEPTH_LABELS,
  type ResearchDepth,
} from './lens-model';
import { errorMessage, explicitProvider, formatTimestamp } from './lens-utils';
import ResearchDepthDial from './ResearchDepthDial';

const LENS_PERIOD = LENS_AUCTION_PERIODS[0];
const defaultClient = new ApiClient();

type LensClient = Pick<ApiClient, 'torque' | 'auction' | 'moneyline'> & Partial<Pick<ApiClient, 'marketSnapshot' | 'providers' | 'optionsChain'>> & LensOverviewClient;
type LensRouter = { push(href: Href): void };
type HapticsLike = Pick<typeof Haptics, 'selectionAsync'>;

type PanelState<T> =
  | { status: 'idle' | 'loading'; data: null; source: null; fetchedAt: null }
  | { status: 'ready'; data: T; source: string; fetchedAt: number; notice?: string }
  | { status: 'unavailable' | 'error'; data: null; source: string | null; fetchedAt: number | null; message: string };

const idlePanel = <T,>(): PanelState<T> => ({ status: 'idle', data: null, source: null, fetchedAt: null });

export type LensScreenProps = {
  symbol: string;
  client?: LensClient;
  preferences?: Preferences;
  cache?: LensOverviewCache;
  reachability?: NetworkReachability;
  router?: LensRouter;
  haptics?: HapticsLike;
  width?: number;
  fontScale?: number;
  now?: () => number;
};

function torqueSource(data: TorqueResponse): string {
  return explicitProvider(isRecord(data.raw) ? data.raw.provider : undefined, data.meta.provider);
}

function auctionSource(data: AuctionResponse): string {
  return explicitProvider(data.provider, data.meta.provider);
}

function moneylineSource(data: MoneylineResponse): string {
  return explicitProvider(data.meta.provider);
}

function ConnectedLensScreen(props: LensScreenProps) {
  const router = useRouter();
  const reachability = useNetworkReachability();
  const { preferences } = usePreferences();
  return <LensController {...props} preferences={props.preferences ?? preferences} reachability={reachability} router={router} />;
}

export default function LensScreen(props: LensScreenProps) {
  return props.router ? <LensController {...props} /> : <ConnectedLensScreen {...props} />;
}

function LensController({
  symbol: rawSymbol,
  client = defaultClient,
  cache,
  preferences = DEFAULT_PREFERENCES,
  reachability = 'unknown',
  router = { push: () => undefined },
  haptics = Haptics,
  width: requestedWidth,
  fontScale: requestedFontScale,
  now = Date.now,
}: LensScreenProps) {
  const window = useWindowDimensions();
  const width = requestedWidth ?? window.width;
  const fontScale = requestedFontScale ?? window.fontScale;
  const compact = width < 350 || fontScale >= 1.3;
  const chartWidth = Math.max(270, Math.min(layout.maximumContentWidth - 44, width - (compact ? 32 : 44)));
  const normalized = normalizeLensSymbol(rawSymbol);
  const symbol = normalized.symbol;
  const [selectedDepth, setSelectedDepth] = useState<ResearchDepth>(preferences.defaultDepth);
  const [openedDepth, setOpenedDepth] = useState<ResearchDepth | null>(null);
  const [chartInterval, setChartInterval] = useState<ChartInterval>(preferences.defaultInterval);
  const [torqueState, setTorqueState] = useState<PanelState<TorqueResponse>>(idlePanel);
  const [auctionState, setAuctionState] = useState<PanelState<ChartDataset>>(idlePanel);
  const [moneylineState, setMoneylineState] = useState<PanelState<MoneylineResponse>>(idlePanel);
  const [optionsState, setOptionsState] = useState<PanelState<OptionsChainResponse>>(idlePanel);
  const torqueCoordinator = useRef(new RequestCoordinator<TorqueResponse>());
  const auctionCoordinator = useRef(new RequestCoordinator<AuctionResponse>());
  const moneylineCoordinator = useRef(new RequestCoordinator<MoneylineResponse>());
  const optionsCoordinator = useRef(new RequestCoordinator<OptionsChainResponse>());
  const torqueGeneration = useRef(0);
  const auctionGeneration = useRef(0);
  const moneylineGeneration = useRef(0);
  const optionsGeneration = useRef(0);

  useEffect(() => () => {
    torqueGeneration.current += 1;
    auctionGeneration.current += 1;
    moneylineGeneration.current += 1;
    optionsGeneration.current += 1;
    torqueCoordinator.current.cancel();
    auctionCoordinator.current.cancel();
    moneylineCoordinator.current.cancel();
    optionsCoordinator.current.cancel();
  }, []);

  async function loadTorque(force = false, interval: ChartInterval = chartInterval) {
    if (!symbol || (!force && ['loading', 'ready'].includes(torqueState.status))) return;
    const generation = ++torqueGeneration.current;
    setTorqueState({ status: 'loading', data: null, source: null, fetchedAt: null });
    try {
      const result = await torqueCoordinator.current.run((signal) =>
        client.torque({ ticker: symbol, period: '2y', interval }, { signal }),
      );
      if (!result.accepted || generation !== torqueGeneration.current) return;
      if (result.value.ticker !== symbol) {
        setTorqueState({
          status: 'unavailable',
          data: null,
          source: torqueSource(result.value),
          fetchedAt: now(),
          message: `Torque response did not match ${symbol}.`,
        });
        return;
      }
      setTorqueState({
        status: 'ready',
        data: result.value,
        source: torqueSource(result.value),
        fetchedAt: now(),
      });
    } catch (error) {
      if (generation !== torqueGeneration.current) return;
      setTorqueState({
        status: 'error',
        data: null,
        source: null,
        fetchedAt: null,
        message: errorMessage(error, 'Torque data could not be loaded.'),
      });
    }
  }

  async function loadAuction(force = false, interval: ChartInterval = chartInterval) {
    if (!symbol || (!force && ['loading', 'ready'].includes(auctionState.status))) return;
    const generation = ++auctionGeneration.current;
    setAuctionState({ status: 'loading', data: null, source: null, fetchedAt: null });
    try {
      const result = await auctionCoordinator.current.run((signal) =>
        client.auction({ ticker: symbol, period: LENS_PERIOD, interval }, { signal }),
      );
      if (!result.accepted || generation !== auctionGeneration.current) return;
      const dataset = result.value.datasets.find((candidate) => candidate.ticker === symbol);
      const notice = result.value.errors.map((entry) => `${entry.ticker}: ${entry.error}`).join('\n') || undefined;
      if (!dataset) {
        setAuctionState({
          status: 'unavailable',
          data: null,
          source: auctionSource(result.value),
          fetchedAt: now(),
          message: result.value.datasets.length
            ? `Auction response did not match ${symbol}.`
            : notice ?? 'Auction data is unavailable for this ticker.',
        });
        return;
      }
      setAuctionState({
        status: 'ready',
        data: dataset,
        source: auctionSource(result.value),
        fetchedAt: now(),
        notice,
      });
    } catch (error) {
      if (generation !== auctionGeneration.current) return;
      setAuctionState({
        status: 'error',
        data: null,
        source: null,
        fetchedAt: null,
        message: errorMessage(error, 'Auction data could not be loaded.'),
      });
    }
  }

  async function loadMoneyline(force = false) {
    if (!symbol || (!force && ['loading', 'ready'].includes(moneylineState.status))) return;
    const generation = ++moneylineGeneration.current;
    setMoneylineState({ status: 'loading', data: null, source: null, fetchedAt: null });
    try {
      const result = await moneylineCoordinator.current.run((signal) => client.moneyline({ ticker: symbol }, { signal }));
      if (!result.accepted || generation !== moneylineGeneration.current) return;
      if (result.value.ticker !== symbol) {
        setMoneylineState({
          status: 'unavailable',
          data: null,
          source: moneylineSource(result.value),
          fetchedAt: now(),
          message: `Moneyline response did not match ${symbol}.`,
        });
        return;
      }
      setMoneylineState({
        status: 'ready',
        data: result.value,
        source: moneylineSource(result.value),
        fetchedAt: now(),
      });
    } catch (error) {
      if (generation !== moneylineGeneration.current) return;
      setMoneylineState({
        status: 'error',
        data: null,
        source: null,
        fetchedAt: null,
        message: errorMessage(error, 'Moneyline data could not be loaded.'),
      });
    }
  }

  async function loadOptions(force = false) {
    if (!symbol || !client.optionsChain || (!force && ['loading', 'ready'].includes(optionsState.status))) return;
    const generation = ++optionsGeneration.current;
    setOptionsState({ status: 'loading', data: null, source: null, fetchedAt: null });
    try {
      const result = await optionsCoordinator.current.run((signal) => client.optionsChain!(symbol, undefined, { signal }));
      if (!result.accepted || generation !== optionsGeneration.current) return;
      if (result.value.ticker !== symbol) {
        setOptionsState({
          status: 'unavailable',
          data: null,
          source: result.value.provider,
          fetchedAt: now(),
          message: `Options response did not match ${symbol}.`,
        });
        return;
      }
      setOptionsState({
        status: 'ready',
        data: result.value,
        source: result.value.provider,
        fetchedAt: now(),
      });
    } catch (error) {
      if (generation !== optionsGeneration.current) return;
      setOptionsState({
        status: 'error',
        data: null,
        source: null,
        fetchedAt: null,
        message: errorMessage(error, 'Options pulse could not be loaded.'),
      });
    }
  }

  function openSelectedDepth() {
    if (!symbol) return;
    setOpenedDepth(selectedDepth);
    if (selectedDepth === 'deep-dive') {
      router.push({
        pathname: '/research',
        params: { symbol, period: '1y', depth: 'deep-dive' },
      });
      return;
    }
    const force = openedDepth === selectedDepth;
    void loadTorque(force);
    void loadAuction(force);
    if (selectedDepth === 'diagnose') {
      void loadMoneyline(force);
      void loadOptions(force);
    }
  }

  const liveQuoteClient = useMemo(
    () => (client.marketSnapshot ? { marketSnapshot: client.marketSnapshot.bind(client) } : null),
    [client],
  );
  const providerStatusClient = useMemo(
    () => (client.providers ? { providers: client.providers.bind(client) } : null),
    [client],
  );
  const optionsChainClient = useMemo(
    () => (client.optionsChain ? { optionsChain: client.optionsChain.bind(client) } : null),
    [client],
  );

  if (!symbol) {
    return (
      <SafeAreaView edges={['bottom', 'left', 'right']} style={styles.safeArea}>
        <View style={styles.invalidContent}>
          <Text accessibilityRole="header" style={styles.invalidTitle}>Ticker Lens</Text>
          <AsyncState title="Invalid ticker symbol" message={normalized.error ?? 'Invalid ticker symbol.'} tone="error" />
        </View>
      </SafeAreaView>
    );
  }

  const actionLabel = selectedDepth === 'deep-dive'
    ? 'Start Deep Dive'
    : `Open ${RESEARCH_DEPTH_LABELS[selectedDepth]}`;

  return (
    <SafeAreaView edges={['bottom', 'left', 'right']} style={styles.safeArea}>
      <ScrollView
        contentContainerStyle={[styles.content, compact && styles.compactContent]}
        contentInsetAdjustmentBehavior="automatic"
        testID="lens-content">
        <View style={styles.masthead}>
          <View style={styles.symbolBlock}>
            <Text style={styles.eyebrow}>TICKER LENS</Text>
            <Text accessibilityRole="header" style={styles.symbol}>{symbol}</Text>
            <Text style={styles.title}>Price first. Deeper research on demand.</Text>
          </View>
          <Ionicons color={colors.mint} name="aperture-outline" size={30} />
        </View>

        <PriceValuePanel
          cache={cache}
          client={client}
          fontScale={fontScale}
          initialInterval={preferences.defaultInterval}
          now={now}
          reachability={reachability}
          symbol={symbol}
          width={chartWidth}
        />

        {liveQuoteClient && preferences.liveQuotes ? <LiveQuoteCard client={liveQuoteClient} symbol={symbol} /> : null}

        {providerStatusClient ? <MarketDataStatusCard client={providerStatusClient} /> : null}

        <LensOverview cache={cache} client={client} key={symbol} now={now} reachability={reachability} symbol={symbol} />

        <View style={styles.stateReadout}>
          <MetricCard label="SELECTED DEPTH" value={RESEARCH_DEPTH_LABELS[selectedDepth]} detail={`Selected depth: ${RESEARCH_DEPTH_LABELS[selectedDepth]}`} />
          <MetricCard label="OPENED DEPTH" value={openedDepth ? RESEARCH_DEPTH_LABELS[openedDepth] : 'None'} detail={`Opened depth: ${openedDepth ? RESEARCH_DEPTH_LABELS[openedDepth] : 'None'}`} accent="mint" />
        </View>

        <ResearchDepthDial
          fontScale={fontScale}
          haptics={haptics}
          onChange={setSelectedDepth}
          selectedDepth={selectedDepth}
          width={width}
        />

        <Text style={styles.depthExplanation}>{RESEARCH_DEPTH_DESCRIPTIONS[selectedDepth]}</Text>
        <Pressable
          accessibilityLabel={actionLabel}
          accessibilityRole="button"
          onPress={openSelectedDepth}
          style={({ pressed }) => [styles.openAction, pressed && styles.pressed]}>
          <View style={styles.openActionCopy}>
            <Text style={styles.openActionEyebrow}>EXPLICIT ACTION</Text>
            <Text style={styles.openActionText}>{actionLabel}</Text>
          </View>
          <Ionicons color={colors.graphite} name="arrow-forward" size={22} />
        </Pressable>

        {openedDepth && openedDepth !== 'deep-dive' ? (
          <View style={styles.panels}>
            <Text accessibilityRole="header" style={styles.panelsTitle}>Opened intelligence</Text>
            <ChartIntervalRail
              testID="opened-intelligence-interval-rail"
              interval={chartInterval}
              onChange={(next) => {
                if (next === chartInterval) return;
                setChartInterval(next);
                void loadTorque(true, next);
                void loadAuction(true, next);
              }}
            />
            <LensPanel title={`${symbol} Torque`} state={torqueState} onRetry={() => void loadTorque(true)}>
              {torqueState.status === 'ready' ? <TorqueChart dataset={torqueState.data} fontScale={fontScale} title={`${symbol} Torque`} width={chartWidth} /> : null}
            </LensPanel>
            <LensPanel title={`${symbol} Auction`} state={auctionState} onRetry={() => void loadAuction(true)}>
              {auctionState.status === 'ready' ? <AuctionChart dataset={auctionState.data} fontScale={fontScale} title={`${symbol} 5d Auction`} width={chartWidth} /> : null}
            </LensPanel>
            {openedDepth === 'diagnose' ? (
              <LensPanel title={`${symbol} Moneyline`} state={moneylineState} onRetry={() => void loadMoneyline(true)}>
                {moneylineState.status === 'ready' ? <MoneylineChart dataset={moneylineState.data} fontScale={fontScale} title={`${symbol} Moneyline`} width={chartWidth} /> : null}
              </LensPanel>
            ) : null}
            {openedDepth === 'diagnose' && optionsChainClient ? (
              <LensPanel title={`${symbol} Options Pulse`} state={optionsState} onRetry={() => void loadOptions(true)}>
                {optionsState.status === 'ready' ? <OptionsPulseCard data={optionsState.data} symbol={symbol} /> : null}
              </LensPanel>
            ) : null}
          </View>
        ) : null}
      </ScrollView>
    </SafeAreaView>
  );
}

function LensPanel<T>({
  title,
  state,
  onRetry,
  children,
}: {
  title: string;
  state: PanelState<T>;
  onRetry: () => void;
  children: React.ReactNode;
}) {
  return (
    <View style={styles.panel}>
      {state.status === 'idle' || state.status === 'loading' ? (
        <AsyncState title={`Loading ${title}`} message="This panel runs independently from the others." />
      ) : null}
      {state.status === 'error' || state.status === 'unavailable' ? (
        <AsyncState
          actionLabel={`Retry ${title}`}
          message={state.message}
          onAction={onRetry}
          title={state.status === 'error' ? `${title} unavailable` : `No ${title} data`}
          tone={state.status === 'error' ? 'error' : 'warning'}
        />
      ) : null}
      {state.status === 'ready' ? (
        <>
          <MetricCard label="PROVENANCE" value={state.source} detail={formatTimestamp(state.fetchedAt)} />
          {state.notice ? <Text accessibilityRole="alert" style={styles.notice}>{state.notice}</Text> : null}
          {children}
        </>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  safeArea: { flex: 1, backgroundColor: colors.graphite },
  content: {
    alignSelf: 'center',
    gap: spacing.lg,
    maxWidth: layout.maximumContentWidth,
    padding: spacing.lg,
    paddingBottom: spacing.xxxl,
    width: '100%',
  },
  compactContent: { paddingHorizontal: spacing.md },
  invalidContent: { alignSelf: 'center', gap: spacing.lg, maxWidth: layout.maximumContentWidth, padding: spacing.lg, width: '100%' },
  invalidTitle: { ...typography.display, color: colors.ink },
  masthead: { alignItems: 'flex-start', flexDirection: 'row', gap: spacing.md, justifyContent: 'space-between' },
  symbolBlock: { flex: 1, gap: spacing.xs },
  eyebrow: { ...typography.eyebrow, color: colors.cyan },
  symbol: { ...typography.display, color: colors.mint },
  title: { ...typography.title, color: colors.ink },
  stateReadout: { gap: spacing.sm },
  depthExplanation: { ...typography.body, color: colors.inkSecondary },
  openAction: {
    alignItems: 'center',
    backgroundColor: colors.mint,
    borderRadius: radii.lg,
    flexDirection: 'row',
    gap: spacing.md,
    justifyContent: 'space-between',
    minHeight: layout.minimumTouchTarget,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.sm,
  },
  openActionCopy: { flex: 1, gap: 2 },
  openActionEyebrow: { ...typography.micro, color: colors.graphiteSoft },
  openActionText: { ...typography.label, color: colors.graphite },
  panels: { gap: spacing.lg },
  panelsTitle: { ...typography.headline, color: colors.ink },
  panel: { gap: spacing.sm },
  notice: { ...typography.caption, color: colors.coral },
  pressed: { opacity: 0.72 },
});
