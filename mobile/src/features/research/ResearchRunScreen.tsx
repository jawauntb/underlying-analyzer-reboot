import Ionicons from '@expo/vector-icons/Ionicons';
import * as Haptics from 'expo-haptics';
import { useRouter } from 'expo-router';
import { useEffect, useRef, useState } from 'react';
import { Pressable, ScrollView, StyleSheet, Text, useWindowDimensions, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { MOBILE_AGENT_TOOLS } from '@/src/api/agentTools';
import { ApiClient, type AgentStreamSession } from '@/src/api/client';
import type { AgentStreamEvent, ToolCatalogResponse } from '@/src/api/contracts';
import {
  defaultLibraryStore,
  type LibraryRecord,
  type LibraryStore,
  type ResearchCompletion,
  type ResearchTraceEntry,
} from '@/src/features/library/library-store';
import { type NetworkReachability, useNetworkReachability } from '@/src/state/network';
import { colors, layout, radii, spacing, typography } from '@/src/theme/tokens';

import {
  buildResearchRequest,
  completionFromAgentResult,
  deriveResearchCapability,
  type ResearchCapability,
  type ResearchPeriod,
} from './research-model';

const defaultClient = new ApiClient();
const TEXT_BATCH_MS = 40;

type ResearchClient = Pick<ApiClient, 'tools' | 'agentStream'>;
type ResearchLibrary = Pick<LibraryStore, 'save' | 'read'>;
type ResearchRouter = { back(): void };
type ResearchHaptics = Pick<typeof Haptics, 'selectionAsync' | 'notificationAsync'>;

type CapabilityState =
  | { status: 'checking'; capability: null; message: string }
  | { status: 'offline'; capability: ResearchCapability | null; message: string }
  | { status: 'ready' | 'unavailable'; capability: ResearchCapability; message: string };

type RunPhase = 'preview' | 'streaming' | 'cancelled' | 'error' | 'completed' | 'saving' | 'saved';

export type ResearchRunScreenProps = {
  symbol: string;
  period: ResearchPeriod;
  recordId?: string | null;
  client?: ResearchClient;
  library?: ResearchLibrary;
  reachability?: NetworkReachability;
  router?: ResearchRouter;
  haptics?: ResearchHaptics;
  now?: () => number;
  width?: number;
};

function errorMessage(error: unknown): string {
  return error instanceof Error && error.message ? error.message : 'Research could not be completed.';
}

function eventRecord(value: unknown): AgentStreamEvent | null {
  if (typeof value !== 'object' || value === null || typeof (value as { type?: unknown }).type !== 'string') return null;
  return value as AgentStreamEvent;
}

function traceAfterEvent(current: readonly ResearchTraceEntry[], event: AgentStreamEvent): ResearchTraceEntry[] {
  if (event.type === 'tool_call') {
    return [...current, { name: event.name, status: 'started', durationMs: null, error: null }];
  }
  if (event.type !== 'tool_result') return [...current];
  const index = current.findIndex((entry) => entry.name === event.name && entry.status === 'started');
  const result: ResearchTraceEntry = {
    name: event.name,
    status: event.ok ? 'completed' : 'failed',
    durationMs: event.durationMs ?? null,
    error: event.error ?? null,
  };
  if (index < 0) return [...current, result];
  return current.map((entry, position) => position === index ? result : entry);
}

function ConnectedResearchRunScreen(props: ResearchRunScreenProps) {
  const reachability = useNetworkReachability();
  const router = useRouter();
  const { width } = useWindowDimensions();
  return <ResearchRunController {...props} reachability={reachability} router={router} width={width} />;
}

export default function ResearchRunScreen(props: ResearchRunScreenProps) {
  const injected = props.reachability !== undefined && props.router !== undefined;
  return injected ? <ResearchRunController {...props} /> : <ConnectedResearchRunScreen {...props} />;
}

function ResearchRunController({
  symbol,
  period,
  recordId = null,
  client = defaultClient,
  library = defaultLibraryStore,
  reachability = 'unknown',
  router = { back: () => undefined },
  haptics = Haptics,
  now = Date.now,
  width = 375,
}: ResearchRunScreenProps) {
  const compact = width < 350;
  const mounted = useRef(true);
  const requestGeneration = useRef(0);
  const capabilityGeneration = useRef(0);
  const activeSession = useRef<AgentStreamSession | null>(null);
  const textTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingText = useRef('');
  const [capability, setCapability] = useState<CapabilityState>(() => reachability === 'offline'
    ? { status: 'offline', capability: null, message: 'Offline · new research is unavailable.' }
    : { status: 'checking', capability: null, message: 'Checking research access…' });
  const [phase, setPhase] = useState<RunPhase>('preview');
  const [visibleText, setVisibleText] = useState('');
  const [trace, setTrace] = useState<ResearchTraceEntry[]>([]);
  const [runError, setRunError] = useState<string | null>(null);
  const [completion, setCompletion] = useState<ResearchCompletion | null>(null);
  const [savedRecord, setSavedRecord] = useState<LibraryRecord | null>(null);
  const [savedError, setSavedError] = useState<string | null>(null);
  const [saveMessage, setSaveMessage] = useState<string | null>(null);
  const [transport, setTransport] = useState<'stream' | 'fallback' | null>(null);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      requestGeneration.current += 1;
      capabilityGeneration.current += 1;
      if (textTimer.current) clearTimeout(textTimer.current);
      textTimer.current = null;
      pendingText.current = '';
      activeSession.current?.cancel();
      activeSession.current = null;
    };
  }, []);

  useEffect(() => {
    const generation = ++capabilityGeneration.current;
    if (recordId) {
      setSavedRecord(null);
      setSavedError(null);
      void library.read(recordId).then((record) => {
        if (!mounted.current || generation !== capabilityGeneration.current) return;
        if (!record || record.symbol !== symbol || record.period !== period) {
          setSavedError('This saved research record is unavailable or does not match the route.');
          return;
        }
        setSavedRecord(record);
      }).catch((error) => {
        if (mounted.current && generation === capabilityGeneration.current) setSavedError(errorMessage(error));
      });
      return;
    }
    if (reachability === 'offline') {
      setCapability((current) => ({
        status: 'offline',
        capability: current.capability,
        message: 'Offline · new research is unavailable.',
      }));
      return;
    }

    const controller = new AbortController();
    setCapability({ status: 'checking', capability: null, message: 'Checking research access…' });
    const timer = setTimeout(() => {
      void client.tools({ signal: controller.signal }).then((catalog: ToolCatalogResponse) => {
        if (!mounted.current || generation !== capabilityGeneration.current) return;
        const next = deriveResearchCapability(catalog);
        setCapability({ status: next.ready ? 'ready' : 'unavailable', capability: next, message: next.message });
      }).catch((error) => {
        if (!mounted.current || generation !== capabilityGeneration.current || controller.signal.aborted) return;
        const unavailable: ResearchCapability = {
          ready: false,
          agentReady: false,
          model: null,
          missingTools: [...MOBILE_AGENT_TOOLS],
          message: errorMessage(error),
        };
        setCapability({ status: 'unavailable', capability: unavailable, message: unavailable.message });
      });
    }, 0);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [client, library, period, reachability, recordId, symbol]);

  function clearTextTimer() {
    if (textTimer.current) clearTimeout(textTimer.current);
    textTimer.current = null;
  }

  function flushText(generation: number) {
    clearTextTimer();
    if (!mounted.current || generation !== requestGeneration.current) {
      pendingText.current = '';
      return;
    }
    const chunk = pendingText.current;
    pendingText.current = '';
    if (chunk) setVisibleText((current) => current + chunk);
  }

  function queueText(text: string, generation: number) {
    if (!text || !mounted.current || generation !== requestGeneration.current) return;
    pendingText.current += text;
    if (textTimer.current) return;
    textTimer.current = setTimeout(() => flushText(generation), TEXT_BATCH_MS);
  }

  function handleEvent(value: unknown, generation: number) {
    if (!mounted.current || generation !== requestGeneration.current) return;
    const event = eventRecord(value);
    if (!event) return;
    if (event.type === 'text') queueText(event.text, generation);
    if (event.type === 'tool_call' || event.type === 'tool_result') {
      setTrace((current) => traceAfterEvent(current, event));
    }
  }

  async function startRun() {
    if (
      recordId
      || reachability === 'offline'
      || capability.status !== 'ready'
      || !capability.capability.ready
      || phase === 'streaming'
    ) return;
    const generation = ++requestGeneration.current;
    clearTextTimer();
    pendingText.current = '';
    setVisibleText('');
    setTrace([]);
    setRunError(null);
    setCompletion(null);
    setSaveMessage(null);
    setTransport(null);
    setPhase('streaming');
    void haptics.selectionAsync().catch(() => undefined);

    try {
      const request = buildResearchRequest({ symbol, period });
      const session = client.agentStream(request, { onEvent: (event) => handleEvent(event, generation) });
      activeSession.current = session;
      const result = await session.result;
      if (!mounted.current || generation !== requestGeneration.current) return;
      flushText(generation);
      const completed = completionFromAgentResult({ result, symbol, period, generatedAt: now() });
      setVisibleText(completed.summary);
      setTrace([...completed.toolTrace]);
      setCompletion(completed);
      setTransport(completed.transport);
      setPhase('completed');
      activeSession.current = null;
      void haptics.notificationAsync(Haptics.NotificationFeedbackType.Success).catch(() => undefined);
    } catch (error) {
      if (!mounted.current || generation !== requestGeneration.current) return;
      flushText(generation);
      activeSession.current = null;
      setRunError(errorMessage(error));
      setPhase('error');
    }
  }

  function cancelRun() {
    if (phase !== 'streaming') return;
    const buffered = pendingText.current;
    requestGeneration.current += 1;
    clearTextTimer();
    pendingText.current = '';
    activeSession.current?.cancel();
    activeSession.current = null;
    if (buffered) setVisibleText((current) => current + buffered);
    setCompletion(null);
    setRunError(null);
    setPhase('cancelled');
    void haptics.selectionAsync().catch(() => undefined);
  }

  async function saveCompletion() {
    if (!completion || phase !== 'completed') return;
    setPhase('saving');
    setRunError(null);
    try {
      const receipt = await library.save(completion);
      if (!mounted.current) return;
      setSaveMessage(receipt.prunedCount > 0
        ? `Saved on this device. ${receipt.prunedCount} older record${receipt.prunedCount === 1 ? '' : 's'} pruned.`
        : 'Saved on this device.');
      setPhase('saved');
      void haptics.notificationAsync(Haptics.NotificationFeedbackType.Success).catch(() => undefined);
    } catch (error) {
      if (!mounted.current) return;
      setRunError(errorMessage(error));
      setPhase('completed');
    }
  }

  function close() {
    if (phase === 'streaming') cancelRun();
    void haptics.selectionAsync().catch(() => undefined);
    router.back();
  }

  const canStart = !recordId
    && reachability !== 'offline'
    && capability.status === 'ready'
    && capability.capability.ready
    && phase !== 'streaming';
  const retry = phase === 'error' || phase === 'cancelled';
  const capabilityLabel = capability.capability
    ? capability.capability.agentReady ? 'agent_ready · YES' : 'agent_ready · NO'
    : 'agent_ready · NOT CHECKED';

  if (recordId) {
    return (
      <SafeAreaView edges={['bottom', 'left', 'right']} style={styles.safeArea}>
        <ScrollView contentContainerStyle={[styles.content, compact && styles.compactContent]} contentInsetAdjustmentBehavior="automatic" testID="research-content">
          <View style={styles.deviceTag}><Ionicons color={colors.cyan} name="archive-outline" size={18} /><Text style={styles.deviceTagText}>On this device</Text></View>
          <Text accessibilityRole="header" style={styles.title}>{symbol} research</Text>
          {!savedRecord && !savedError ? <Text style={styles.body}>Opening saved research…</Text> : null}
          {savedError ? <View accessibilityRole="alert" style={styles.errorCard}><Text style={styles.errorText}>{savedError}</Text></View> : null}
          {savedRecord ? (
            <>
              <Text style={styles.summary}>{savedRecord.summary || 'No written summary was returned.'}</Text>
              <View style={styles.metaCard}>
                <Text style={styles.metaLabel}>SOURCE</Text>
                <Text style={styles.metaValue}>{savedRecord.source.transport === 'stream' ? 'Research stream' : 'Non-streaming fallback'}</Text>
                <Text style={styles.metaNote}>Generated {new Date(savedRecord.generatedAt).toLocaleString()} · cached {new Date(savedRecord.cachedAt).toLocaleString()}</Text>
              </View>
              <TraceList trace={savedRecord.toolTrace} />
            </>
          ) : null}
          <SecondaryButton accessibilityLabel="Close Research Run preview" label="Close" onPress={close} />
        </ScrollView>
      </SafeAreaView>
    );
  }

  return (
    <SafeAreaView edges={['bottom', 'left', 'right']} style={styles.safeArea}>
      <ScrollView contentContainerStyle={[styles.content, compact && styles.compactContent]} contentInsetAdjustmentBehavior="automatic" testID="research-content">
        <View accessibilityLabel="Research Run preview placeholder" style={styles.previewTag}>
          <Ionicons color={colors.coral} name="radio-outline" size={18} />
          <Text style={styles.previewText}>EXPLICIT RESEARCH RUN</Text>
        </View>
        <Text accessibilityRole="header" style={styles.title}>{symbol} · {period}</Text>
        <Text style={styles.body}>One bounded run will inspect company facts, filings, news, charts, and provider health. Nothing starts from opening this sheet.</Text>

        <View accessibilityLabel="Research capability preview" style={styles.metaCard}>
          <Text style={styles.metaLabel}>CAPABILITY</Text>
          <Text style={styles.metaValue}>{capabilityLabel}</Text>
          <Text style={[styles.metaNote, capability.status === 'unavailable' && styles.errorText]}>{capability.message}</Text>
        </View>

        <View style={styles.toolCard}>
          <Text style={styles.metaLabel}>THIS RUN CAN USE</Text>
          <View style={styles.toolGrid}>
            {MOBILE_AGENT_TOOLS.map((tool) => <Text key={tool} style={styles.toolName}>{tool}</Text>)}
          </View>
          <Text style={styles.boundaryNote}>Articles stay outside this run; compose and image tools are not selected.</Text>
        </View>

        {phase === 'streaming' ? <View accessibilityLiveRegion="polite" style={styles.phaseCard}><Text style={styles.phaseLabel}>RUNNING</Text><Text style={styles.phaseValue}>Research is streaming…</Text></View> : null}
        {phase === 'cancelled' ? <View accessibilityRole="alert" style={styles.phaseCard}><Text style={styles.phaseValue}>Research cancelled.</Text><Text style={styles.metaNote}>Partial text stays on screen but cannot be saved.</Text></View> : null}
        {runError ? <View accessibilityRole="alert" style={styles.errorCard}><Text style={styles.errorText}>{runError}</Text><Text style={styles.metaNote}>Nothing was retried automatically.</Text></View> : null}
        {visibleText ? <Text accessibilityLabel="Research summary" style={styles.summary}>{visibleText}</Text> : null}
        {trace.length ? <TraceList trace={trace} /> : null}
        {transport ? <Text style={styles.transport}>{transport === 'stream' ? 'Streaming transport' : 'Non-streaming fallback'}</Text> : null}
        {saveMessage ? <Text accessibilityLiveRegion="polite" style={styles.savedMessage}>{saveMessage}</Text> : null}

        {phase === 'streaming' ? (
          <SecondaryButton accessibilityLabel="Cancel Research Run" label="Cancel run" onPress={cancelRun} tone="coral" />
        ) : ['preview', 'error', 'cancelled'].includes(phase) ? (
          <PrimaryButton
            accessibilityLabel={`${retry ? 'Retry' : 'Start'} ${symbol} Research Run`}
            disabled={!canStart}
            label={retry ? 'Retry research' : 'Start research'}
            onPress={() => void startRun()}
          />
        ) : null}
        {phase === 'completed' || phase === 'saving' ? (
          <PrimaryButton
            accessibilityLabel={`Save ${symbol} research on this device`}
            disabled={phase === 'saving'}
            label={phase === 'saving' ? 'Saving…' : 'Save on this device'}
            onPress={() => void saveCompletion()}
          />
        ) : null}
        <SecondaryButton accessibilityLabel="Close Research Run preview" label="Close" onPress={close} />
      </ScrollView>
    </SafeAreaView>
  );
}

function TraceList({ trace }: { trace: readonly ResearchTraceEntry[] }) {
  return (
    <View accessibilityLabel="Research tool trace" style={styles.traceCard}>
      <Text style={styles.metaLabel}>TOOL TRACE</Text>
      {trace.map((entry, index) => (
        <View key={`${entry.name}-${index}`} style={styles.traceRow}>
          <View style={[styles.traceDot, entry.status === 'completed' ? styles.traceOk : entry.status === 'failed' ? styles.traceFailed : styles.traceRunning]} />
          <View style={styles.traceCopy}>
            <Text style={styles.traceName}>{entry.name} · {entry.status}</Text>
            {entry.durationMs !== null ? <Text style={styles.traceMeta}>{entry.durationMs} ms</Text> : null}
            {entry.error ? <Text style={styles.errorText}>{entry.error}</Text> : null}
          </View>
        </View>
      ))}
    </View>
  );
}

function PrimaryButton({ accessibilityLabel, disabled = false, label, onPress }: {
  accessibilityLabel: string;
  disabled?: boolean;
  label: string;
  onPress(): void;
}) {
  return (
    <Pressable accessibilityLabel={accessibilityLabel} accessibilityRole="button" accessibilityState={{ disabled }} disabled={disabled} onPress={onPress} style={({ pressed }) => [styles.primaryAction, disabled && styles.disabled, pressed && styles.pressed]}>
      <Text style={styles.primaryActionText}>{label}</Text>
    </Pressable>
  );
}

function SecondaryButton({ accessibilityLabel, label, onPress, tone = 'default' }: {
  accessibilityLabel: string;
  label: string;
  onPress(): void;
  tone?: 'default' | 'coral';
}) {
  return (
    <Pressable accessibilityLabel={accessibilityLabel} accessibilityRole="button" onPress={onPress} style={({ pressed }) => [styles.secondaryAction, tone === 'coral' && styles.coralAction, pressed && styles.pressed]}>
      <Text style={[styles.secondaryActionText, tone === 'coral' && styles.coralText]}>{label}</Text>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  safeArea: { backgroundColor: colors.graphiteRaised, flex: 1 },
  content: { alignSelf: 'center', gap: spacing.md, maxWidth: layout.maximumContentWidth, padding: spacing.lg, paddingBottom: spacing.xxxl, width: '100%' },
  compactContent: { paddingHorizontal: spacing.md },
  previewTag: { alignItems: 'center', alignSelf: 'flex-start', backgroundColor: colors.mineralSoft, borderRadius: radii.pill, flexDirection: 'row', gap: spacing.xs, minHeight: layout.minimumTouchTarget, paddingHorizontal: spacing.sm },
  previewText: { ...typography.micro, color: colors.inkSecondary },
  deviceTag: { alignItems: 'center', alignSelf: 'flex-start', flexDirection: 'row', gap: spacing.xs, minHeight: layout.minimumTouchTarget },
  deviceTagText: { ...typography.label, color: colors.cyan },
  title: { ...typography.display, color: colors.ink },
  body: { ...typography.body, color: colors.inkSecondary },
  metaCard: { backgroundColor: colors.mineralSoft, borderColor: colors.mineral, borderRadius: radii.lg, borderWidth: 1, gap: spacing.xs, padding: spacing.md },
  metaLabel: { ...typography.eyebrow, color: colors.inkMuted },
  metaValue: { ...typography.headline, color: colors.ink },
  metaNote: { ...typography.caption, color: colors.inkSecondary },
  toolCard: { borderColor: colors.mineral, borderRadius: radii.xl, borderWidth: 1, gap: spacing.sm, padding: spacing.md },
  toolGrid: { flexDirection: 'row', flexWrap: 'wrap', gap: spacing.xs },
  toolName: { ...typography.micro, backgroundColor: colors.graphiteSoft, borderRadius: radii.pill, color: colors.mint, minHeight: 30, paddingHorizontal: spacing.sm, paddingVertical: spacing.xs },
  boundaryNote: { ...typography.caption, color: colors.cyan },
  phaseCard: { borderColor: colors.cyan, borderRadius: radii.lg, borderWidth: 1, gap: spacing.xs, padding: spacing.md },
  phaseLabel: { ...typography.eyebrow, color: colors.cyan },
  phaseValue: { ...typography.title, color: colors.ink },
  errorCard: { borderColor: colors.coral, borderRadius: radii.lg, borderWidth: 1, gap: spacing.xs, padding: spacing.md },
  errorText: { ...typography.caption, color: colors.coral },
  summary: { ...typography.body, backgroundColor: colors.graphite, borderColor: colors.mineral, borderRadius: radii.lg, borderWidth: 1, color: colors.ink, padding: spacing.md },
  traceCard: { borderColor: colors.mineral, borderRadius: radii.lg, borderWidth: 1, gap: spacing.sm, padding: spacing.md },
  traceRow: { alignItems: 'flex-start', flexDirection: 'row', gap: spacing.sm, minHeight: layout.minimumTouchTarget, paddingVertical: spacing.xs },
  traceDot: { borderRadius: 5, height: 10, marginTop: 6, width: 10 },
  traceOk: { backgroundColor: colors.mint },
  traceFailed: { backgroundColor: colors.coral },
  traceRunning: { backgroundColor: colors.cyan },
  traceCopy: { flex: 1, gap: 2 },
  traceName: { ...typography.label, color: colors.ink },
  traceMeta: { ...typography.caption, color: colors.inkMuted },
  transport: { ...typography.caption, color: colors.cyan },
  savedMessage: { ...typography.label, color: colors.mint },
  primaryAction: { alignItems: 'center', backgroundColor: colors.mint, borderRadius: radii.md, justifyContent: 'center', minHeight: layout.minimumTouchTarget, paddingHorizontal: spacing.md, paddingVertical: spacing.sm },
  primaryActionText: { ...typography.label, color: colors.graphite },
  secondaryAction: { alignItems: 'center', borderColor: colors.mineral, borderRadius: radii.md, borderWidth: 1, justifyContent: 'center', minHeight: layout.minimumTouchTarget, paddingHorizontal: spacing.md, paddingVertical: spacing.sm },
  secondaryActionText: { ...typography.label, color: colors.ink },
  coralAction: { borderColor: colors.coral },
  coralText: { color: colors.coral },
  disabled: { opacity: 0.45 },
  pressed: { opacity: 0.72 },
});
