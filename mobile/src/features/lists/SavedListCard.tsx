import Ionicons from '@expo/vector-icons/Ionicons';
import { useState } from 'react';
import { Pressable, StyleSheet, Text, TextInput, View } from 'react-native';

import { colors, layout, radii, spacing, typography } from '@/src/theme/tokens';

import type { SavedList } from './watchlists';

type SavedListCardProps = {
  list: SavedList;
  onOpenSymbol(symbol: string): void;
  onRename(name: string): Promise<unknown>;
  onAddSymbol(symbol: string): Promise<unknown>;
  onRemoveSymbol(symbol: string): Promise<unknown>;
  onDelete(): Promise<unknown>;
};

function message(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

export default function SavedListCard({
  list,
  onOpenSymbol,
  onRename,
  onAddSymbol,
  onRemoveSymbol,
  onDelete,
}: SavedListCardProps) {
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(list.name);
  const [symbol, setSymbol] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState(false);

  async function run(action: () => Promise<unknown>, fallback: string, after?: () => void) {
    setError(null);
    try {
      await action();
      after?.();
    } catch (failure) {
      setError(message(failure, fallback));
    }
  }

  function toggleEditing() {
    setError(null);
    setConfirmingDelete(false);
    setSymbol('');
    setName(list.name);
    setEditing((current) => !current);
  }

  return (
    <View accessibilityLabel={`Saved list ${list.name}`} style={styles.card}>
      <View style={styles.heading}>
        <View style={styles.headingCopy}>
          {editing ? (
            <TextInput
              accessibilityLabel={`Rename ${list.name}`}
              onChangeText={setName}
              onSubmitEditing={() => void run(() => onRename(name), 'The list could not be renamed.')}
              placeholderTextColor={colors.inkMuted}
              style={styles.nameInput}
              value={name}
            />
          ) : (
            <Text style={styles.name}>{list.name}</Text>
          )}
          <Text style={styles.meta}>
            {list.source.kind === 'manual' ? 'MANUAL' : `TRADINGVIEW · ${list.source.remoteId}`}
            {` · ${list.symbols.length} ${list.symbols.length === 1 ? 'SYMBOL' : 'SYMBOLS'}`}
          </Text>
        </View>
        <Pressable
          accessibilityLabel={editing ? `Done editing ${list.name}` : `Edit ${list.name}`}
          accessibilityRole="button"
          onPress={toggleEditing}
          style={({ pressed }) => [styles.iconAction, pressed && styles.pressed]}>
          <Ionicons color={editing ? colors.mint : colors.inkMuted} name={editing ? 'checkmark' : 'create-outline'} size={20} />
        </Pressable>
      </View>

      {editing ? (
        <Pressable
          accessibilityLabel={`Save name for ${list.name}`}
          accessibilityRole="button"
          onPress={() => void run(() => onRename(name), 'The list could not be renamed.')}
          style={({ pressed }) => [styles.secondaryAction, pressed && styles.pressed]}>
          <Text style={styles.secondaryActionText}>Save name</Text>
        </Pressable>
      ) : null}

      <View style={styles.symbols}>
        {list.symbols.map((entry) => (
          <View key={entry} style={styles.symbolRow}>
            <Pressable
              accessibilityLabel={`Open ${entry} Lens`}
              accessibilityRole="button"
              onPress={() => onOpenSymbol(entry)}
              style={({ pressed }) => [styles.symbolAction, pressed && styles.pressed]}>
              <Text style={styles.symbolText}>{entry}</Text>
              <Ionicons color={colors.cyan} name="arrow-forward" size={18} />
            </Pressable>
            {editing ? (
              <Pressable
                accessibilityLabel={`Remove ${entry} from ${list.name}`}
                accessibilityRole="button"
                onPress={() => void run(() => onRemoveSymbol(entry), `${entry} could not be removed.`)}
                style={({ pressed }) => [styles.iconAction, pressed && styles.pressed]}>
                <Ionicons color={colors.coral} name="close" size={20} />
              </Pressable>
            ) : null}
          </View>
        ))}
      </View>

      {editing ? (
        <View style={styles.editor}>
          <View style={styles.addRow}>
            <TextInput
              accessibilityLabel={`New symbol for ${list.name}`}
              autoCapitalize="characters"
              autoCorrect={false}
              onChangeText={setSymbol}
              onSubmitEditing={() => void run(() => onAddSymbol(symbol), 'The symbol could not be added.', () => setSymbol(''))}
              placeholder="TSLA"
              placeholderTextColor={colors.inkMuted}
              style={styles.symbolInput}
              value={symbol}
            />
            <Pressable
              accessibilityLabel={`Add ${symbol.trim() ? symbol.trim().toUpperCase() : 'symbol'} to ${list.name}`}
              accessibilityRole="button"
              onPress={() => void run(() => onAddSymbol(symbol), 'The symbol could not be added.', () => setSymbol(''))}
              style={({ pressed }) => [styles.addAction, pressed && styles.pressed]}>
              <Text style={styles.addActionText}>Add</Text>
            </Pressable>
          </View>

          <Pressable
            accessibilityLabel={confirmingDelete ? `Confirm delete ${list.name}` : `Delete ${list.name}`}
            accessibilityRole="button"
            onPress={() => {
              if (!confirmingDelete) {
                setError(null);
                setConfirmingDelete(true);
                return;
              }
              void run(onDelete, 'The list could not be deleted.');
            }}
            style={({ pressed }) => [styles.deleteAction, confirmingDelete && styles.deleteActionArmed, pressed && styles.pressed]}>
            <Text style={[styles.deleteActionText, confirmingDelete && styles.deleteActionTextArmed]}>
              {confirmingDelete ? `Confirm delete · ${list.symbols.length} symbols` : 'Delete list'}
            </Text>
          </Pressable>
        </View>
      ) : null}

      {error ? <Text accessibilityRole="alert" style={styles.error}>{error}</Text> : null}
    </View>
  );
}

const styles = StyleSheet.create({
  card: {
    backgroundColor: colors.graphiteRaised,
    borderColor: colors.mineral,
    borderRadius: radii.lg,
    borderWidth: 1,
    gap: spacing.sm,
    padding: spacing.md,
  },
  heading: { alignItems: 'flex-start', flexDirection: 'row', gap: spacing.sm, justifyContent: 'space-between' },
  headingCopy: { flex: 1, gap: spacing.xs },
  name: { ...typography.title, color: colors.ink },
  nameInput: {
    ...typography.title,
    backgroundColor: colors.graphite,
    borderColor: colors.mineral,
    borderRadius: radii.md,
    borderWidth: 1,
    color: colors.ink,
    minHeight: layout.minimumTouchTarget,
    paddingHorizontal: spacing.sm,
  },
  meta: { ...typography.micro, color: colors.inkMuted },
  iconAction: {
    alignItems: 'center',
    justifyContent: 'center',
    minHeight: layout.minimumTouchTarget,
    minWidth: layout.minimumTouchTarget,
  },
  symbols: { gap: spacing.xs },
  symbolRow: { alignItems: 'center', flexDirection: 'row', gap: spacing.sm },
  symbolAction: {
    alignItems: 'center',
    borderTopColor: colors.mineral,
    borderTopWidth: 1,
    flex: 1,
    flexDirection: 'row',
    justifyContent: 'space-between',
    minHeight: layout.minimumTouchTarget,
    paddingHorizontal: spacing.sm,
  },
  symbolText: { ...typography.label, color: colors.ink },
  editor: { borderTopColor: colors.mineral, borderTopWidth: 1, gap: spacing.sm, paddingTop: spacing.sm },
  addRow: { alignItems: 'center', flexDirection: 'row', gap: spacing.sm },
  symbolInput: {
    ...typography.body,
    backgroundColor: colors.graphite,
    borderColor: colors.mineral,
    borderRadius: radii.md,
    borderWidth: 1,
    color: colors.ink,
    flex: 1,
    minHeight: layout.minimumTouchTarget,
    paddingHorizontal: spacing.sm,
  },
  addAction: {
    alignItems: 'center',
    backgroundColor: colors.mint,
    borderRadius: radii.md,
    justifyContent: 'center',
    minHeight: layout.minimumTouchTarget,
    paddingHorizontal: spacing.md,
  },
  addActionText: { ...typography.label, color: colors.graphite },
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
  deleteAction: {
    alignItems: 'center',
    borderColor: colors.mineral,
    borderRadius: radii.md,
    borderWidth: 1,
    justifyContent: 'center',
    minHeight: layout.minimumTouchTarget,
    paddingHorizontal: spacing.md,
  },
  deleteActionArmed: { backgroundColor: colors.coral, borderColor: colors.coral },
  deleteActionText: { ...typography.label, color: colors.coral },
  deleteActionTextArmed: { color: colors.graphite },
  error: { ...typography.caption, color: colors.coral },
  pressed: { opacity: 0.72 },
});
