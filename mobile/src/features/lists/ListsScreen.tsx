import { useRouter, type Href } from 'expo-router';
import { useEffect, useRef, useState } from 'react';
import { Pressable, ScrollView, StyleSheet, Text, TextInput, useWindowDimensions, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { ApiClient, RequestCoordinator } from '@/src/api/client';
import type { ResolveWatchlistResponse } from '@/src/api/contracts';
import AsyncState from '@/src/components/ui/AsyncState';
import { colors, layout, radii, spacing, typography } from '@/src/theme/tokens';

import SavedListCard from './SavedListCard';
import {
  normalizeListSymbols,
  type SavedListsContextValue,
  useSavedLists,
  validateTradingViewUrl,
} from './watchlists';

const defaultClient = new ApiClient();

type ListsClient = Pick<ApiClient, 'resolveWatchlist'>;
type ListsRouter = { push(href: Href): void };

type Preview = {
  name: string;
  symbols: string[];
  sourceUrl: string;
  remoteId: string;
};

export type ListsScreenProps = {
  client?: ListsClient;
  listsState?: SavedListsContextValue;
  router?: ListsRouter;
};

function errorMessage(error: unknown): string {
  return error instanceof Error && error.message ? error.message : 'The watchlist could not be resolved.';
}

function ConnectedListsScreen(props: ListsScreenProps) {
  const listsState = useSavedLists();
  const router = useRouter();
  return <ListsController {...props} listsState={listsState} router={router} />;
}

export default function ListsScreen(props: ListsScreenProps) {
  return props.listsState && props.router ? <ListsController {...props} /> : <ConnectedListsScreen {...props} />;
}

function ListsController({
  client = defaultClient,
  listsState,
  router = { push: () => undefined },
}: ListsScreenProps) {
  const { width } = useWindowDimensions();
  const compact = width < 350;
  const previewCoordinator = useRef(new RequestCoordinator<ResolveWatchlistResponse>());
  const previewGeneration = useRef(0);
  const [manualName, setManualName] = useState('');
  const [manualSymbols, setManualSymbols] = useState('');
  const [manualError, setManualError] = useState<string | null>(null);
  const [manualSaved, setManualSaved] = useState<string | null>(null);
  const [url, setUrl] = useState('');
  const [preview, setPreview] = useState<Preview | null>(null);
  const [previewName, setPreviewName] = useState('');
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [importSaved, setImportSaved] = useState<string | null>(null);
  const hydrationPending = listsState?.hydrated !== true;

  useEffect(() => () => previewCoordinator.current.cancel(), []);

  async function saveManual() {
    setManualError(null);
    setManualSaved(null);
    try {
      const name = manualName.trim();
      if (!name) throw new Error('List name is required.');
      const symbols = normalizeListSymbols(manualSymbols);
      await listsState?.saveManual(name, symbols);
      setManualSaved(`${name} saved as a new list.`);
      setManualName('');
      setManualSymbols('');
    } catch (error) {
      setManualError(errorMessage(error));
    }
  }

  async function previewImport() {
    setPreviewError(null);
    setImportSaved(null);
    let localSource: ReturnType<typeof validateTradingViewUrl>;
    try {
      localSource = validateTradingViewUrl(url);
    } catch (error) {
      setPreviewError(errorMessage(error));
      return;
    }

    const generation = ++previewGeneration.current;
    setPreviewing(true);
    try {
      const result = await previewCoordinator.current.run((signal) =>
        client.resolveWatchlist(
          { watchlistUrl: localSource.sourceUrl, maxResults: 10 },
          { signal },
        ),
      );
      if (!result.accepted || generation !== previewGeneration.current) return;
      const remoteSource = validateTradingViewUrl(result.value.watchlist.sourceUrl);
      if (remoteSource.remoteId !== localSource.remoteId) {
        throw new Error('Resolved TradingView watchlist does not match the requested list.');
      }
      const symbols = normalizeListSymbols(result.value.tickers);
      setPreview({
        name: result.value.watchlist.name.trim() || `TradingView ${remoteSource.remoteId}`,
        symbols,
        sourceUrl: remoteSource.sourceUrl,
        remoteId: remoteSource.remoteId,
      });
      setPreviewName(result.value.watchlist.name.trim() || `TradingView ${remoteSource.remoteId}`);
    } catch (error) {
      if (generation === previewGeneration.current) {
        setPreview(null);
        setPreviewError(errorMessage(error));
      }
    } finally {
      if (generation === previewGeneration.current) setPreviewing(false);
    }
  }

  async function saveImport() {
    if (!preview) return;
    setPreviewError(null);
    setImportSaved(null);
    try {
      const name = previewName.trim();
      if (!name) throw new Error('Preview list name is required.');
      await listsState?.saveTradingView({
        name,
        symbols: preview.symbols,
        sourceUrl: preview.sourceUrl,
        remoteId: preview.remoteId,
      });
      setImportSaved(`${name} saved as a new list.`);
      setPreview(null);
      setPreviewName('');
      setUrl('');
    } catch (error) {
      setPreviewError(errorMessage(error));
    }
  }

  return (
    <SafeAreaView edges={['top', 'left', 'right']} style={styles.safeArea}>
      <ScrollView contentContainerStyle={[styles.content, compact && styles.compactContent]} contentInsetAdjustmentBehavior="automatic" keyboardShouldPersistTaps="handled">
        <Text style={styles.eyebrow}>YOUR FIELD NOTES</Text>
        <Text accessibilityRole="header" style={styles.title}>Lists</Text>
        <Text style={styles.intro}>Build a deliberate 1–10 symbol queue or preview a public TradingView watchlist.</Text>

        <View style={styles.section}>
          <Text style={styles.sectionEyebrow}>MANUAL LIST</Text>
          <Text style={styles.sectionTitle}>Choose symbols</Text>
          <TextInput
            accessibilityLabel="Manual list name"
            onChangeText={setManualName}
            placeholder="List name"
            placeholderTextColor={colors.inkMuted}
            style={styles.input}
            value={manualName}
          />
          <TextInput
            accessibilityLabel="Manual symbols"
            autoCapitalize="characters"
            multiline
            onChangeText={setManualSymbols}
            placeholder="AAPL, MSFT, NVDA"
            placeholderTextColor={colors.inkMuted}
            style={[styles.input, styles.symbolInput]}
            value={manualSymbols}
          />
          {manualError ? <Text accessibilityRole="alert" style={styles.error}>{manualError}</Text> : null}
          {manualSaved ? <Text style={styles.success}>{manualSaved}</Text> : null}
          <Pressable accessibilityLabel="Save manual list" accessibilityRole="button" accessibilityState={{ disabled: hydrationPending }} disabled={hydrationPending} onPress={() => void saveManual()} style={({ pressed }) => [styles.primaryAction, hydrationPending && styles.disabled, pressed && styles.pressed]}>
            <Text style={styles.primaryActionText}>Save manual list</Text>
          </Pressable>
        </View>

        <View style={styles.section}>
          <Text style={styles.sectionEyebrow}>TRADINGVIEW IMPORT</Text>
          <Text style={styles.sectionTitle}>Preview before saving</Text>
          <TextInput
            accessibilityLabel="TradingView watchlist URL"
            autoCapitalize="none"
            autoCorrect={false}
            keyboardType="url"
            onChangeText={setUrl}
            placeholder="https://www.tradingview.com/watchlists/123/"
            placeholderTextColor={colors.inkMuted}
            style={styles.input}
            value={url}
          />
          <Pressable
            accessibilityLabel="Preview import"
            accessibilityRole="button"
            accessibilityState={{ busy: previewing }}
            onPress={() => void previewImport()}
            style={({ pressed }) => [styles.secondaryAction, pressed && styles.pressed]}>
            <Text style={styles.secondaryActionText}>{previewing ? 'Previewing…' : 'Preview import'}</Text>
          </Pressable>
          {previewError ? <Text accessibilityRole="alert" style={styles.error}>{previewError}</Text> : null}
          {importSaved ? <Text style={styles.success}>{importSaved}</Text> : null}

          {preview ? (
            <View accessibilityLabel="TradingView import preview" style={styles.preview}>
              <Text style={styles.previewLabel}>BACKEND PREVIEW · MAX 10</Text>
              <TextInput
                accessibilityLabel="Preview list name"
                onChangeText={setPreviewName}
                style={styles.input}
                value={previewName}
              />
              <Text style={styles.previewSymbols}>{preview.symbols.join(', ')}</Text>
              <Text style={styles.source}>{preview.sourceUrl}</Text>
              <Pressable accessibilityLabel="Save as new list" accessibilityRole="button" accessibilityState={{ disabled: hydrationPending }} disabled={hydrationPending} onPress={() => void saveImport()} style={({ pressed }) => [styles.primaryAction, hydrationPending && styles.disabled, pressed && styles.pressed]}>
                <Text style={styles.primaryActionText}>Save as new list</Text>
              </Pressable>
            </View>
          ) : null}
        </View>

        <View style={styles.savedSection}>
          <Text style={styles.sectionEyebrow}>ON THIS DEVICE</Text>
          <Text style={styles.sectionTitle}>Saved lists</Text>
          {listsState?.hydrated && listsState.droppedCorruptListCount > 0 ? (
            <AsyncState
              message={`${listsState.droppedCorruptListCount} unreadable saved ${listsState.droppedCorruptListCount === 1 ? 'list was' : 'lists were'} removed. Your other lists are still here.`}
              title="Saved lists repaired"
              tone="warning"
            />
          ) : null}
          {listsState?.hydrationError ? (
            <AsyncState
              actionLabel="Retry saved lists"
              message={listsState.hydrationError}
              onAction={listsState.retryHydration}
              title="Saved lists unavailable"
              tone="error"
            />
          ) : !listsState?.hydrated ? (
            <AsyncState title="Loading saved lists" message="Reading local storage without blocking navigation." />
          ) : listsState.lists.length === 0 ? (
            <AsyncState title="No lists yet" message="Save a manual list or preview a public TradingView watchlist above." />
          ) : (
            listsState.lists.map((list) => (
              <SavedListCard
                key={list.id}
                list={list}
                onAddSymbol={(symbol) => listsState.addSymbol(list.id, symbol)}
                onDelete={() => listsState.deleteList(list.id)}
                onOpenSymbol={(symbol) => router.push({ pathname: '/ticker/[symbol]', params: { symbol } })}
                onRemoveSymbol={(symbol) => listsState.removeSymbol(list.id, symbol)}
                onRename={(name) => listsState.renameList(list.id, name)}
              />
            ))
          )}
        </View>
      </ScrollView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safeArea: { flex: 1, backgroundColor: colors.graphite },
  content: { alignSelf: 'center', gap: spacing.lg, maxWidth: layout.maximumContentWidth, paddingBottom: spacing.xxxl, paddingHorizontal: spacing.lg, paddingTop: spacing.md, width: '100%' },
  compactContent: { paddingHorizontal: spacing.md },
  eyebrow: { ...typography.eyebrow, color: colors.coral },
  title: { ...typography.display, color: colors.ink, marginTop: -spacing.sm },
  intro: { ...typography.body, color: colors.inkSecondary, marginTop: -spacing.md },
  section: { backgroundColor: colors.graphiteRaised, borderColor: colors.mineral, borderRadius: radii.xl, borderWidth: 1, gap: spacing.sm, padding: spacing.md },
  sectionEyebrow: { ...typography.eyebrow, color: colors.cyan },
  sectionTitle: { ...typography.title, color: colors.ink },
  input: { ...typography.body, backgroundColor: colors.graphite, borderColor: colors.mineral, borderRadius: radii.md, borderWidth: 1, color: colors.ink, minHeight: layout.minimumTouchTarget, paddingHorizontal: spacing.md, paddingVertical: spacing.sm },
  symbolInput: { minHeight: 72, textAlignVertical: 'top' },
  primaryAction: { alignItems: 'center', backgroundColor: colors.mint, borderRadius: radii.md, justifyContent: 'center', minHeight: layout.minimumTouchTarget, paddingHorizontal: spacing.md, paddingVertical: spacing.sm },
  primaryActionText: { ...typography.label, color: colors.graphite },
  secondaryAction: { alignItems: 'center', borderColor: colors.mineral, borderRadius: radii.md, borderWidth: 1, justifyContent: 'center', minHeight: layout.minimumTouchTarget, paddingHorizontal: spacing.md, paddingVertical: spacing.sm },
  secondaryActionText: { ...typography.label, color: colors.ink },
  error: { ...typography.caption, color: colors.coral },
  success: { ...typography.caption, color: colors.mint },
  preview: { borderTopColor: colors.mineral, borderTopWidth: 1, gap: spacing.sm, marginTop: spacing.xs, paddingTop: spacing.md },
  previewLabel: { ...typography.micro, color: colors.inkMuted },
  previewSymbols: { ...typography.body, color: colors.ink },
  source: { ...typography.caption, color: colors.inkMuted },
  savedSection: { gap: spacing.sm },
  disabled: { opacity: 0.45 },
  pressed: { opacity: 0.72 },
});
