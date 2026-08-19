import { FlatList, Modal, Pressable, StyleSheet, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { colors, layout, radii, spacing, typography } from '@/src/theme/tokens';

export type ChartDataCell = { label: string; value: string };
export type ChartDataRow = { key: string; label: string; cells: ChartDataCell[] };

type ChartDataTableProps = {
  onClose: () => void;
  rows: readonly ChartDataRow[];
  title: string;
  visible: boolean;
};

export function chartRowAccessibilityText(row: ChartDataRow): string {
  const values = row.cells.map((cell) => `${cell.label} ${cell.value}`).join(', ');
  return values ? `${row.label}. ${values}` : row.label;
}

export function ChartDataTable({ onClose, rows, title, visible }: ChartDataTableProps) {
  return (
    <Modal
      animationType="none"
      onRequestClose={onClose}
      presentationStyle="pageSheet"
      transparent={false}
      visible={visible}>
      <SafeAreaView style={styles.safeArea}>
        <View style={styles.header}>
          <View style={styles.headingCopy}>
            <Text accessibilityRole="header" style={styles.title}>
              {title} data
            </Text>
            <Text style={styles.count}>{rows.length} normalized values</Text>
          </View>
          <Pressable
            accessibilityLabel={`Close ${title} data`}
            accessibilityRole="button"
            onPress={onClose}
            style={({ pressed }) => [styles.close, pressed && styles.pressed]}>
            <Text style={styles.closeText}>Close</Text>
          </Pressable>
        </View>
        <FlatList
          contentContainerStyle={styles.list}
          data={rows}
          keyExtractor={(row) => row.key}
          ListEmptyComponent={<Text style={styles.empty}>No normalized values are available.</Text>}
          renderItem={({ item }) => (
            <View accessible accessibilityLabel={chartRowAccessibilityText(item)} style={styles.row}>
              <Text style={styles.rowLabel}>{item.label}</Text>
              <View style={styles.cells}>
                {item.cells.map((cell) => (
                  <View key={`${item.key}-${cell.label}`} style={styles.cell}>
                    {item.cells.length > 1 ? <Text style={styles.cellLabel}>{cell.label}</Text> : null}
                    <Text style={styles.cellValue}>{cell.value}</Text>
                  </View>
                ))}
              </View>
            </View>
          )}
        />
      </SafeAreaView>
    </Modal>
  );
}

const styles = StyleSheet.create({
  safeArea: { backgroundColor: colors.graphite, flex: 1 },
  header: {
    alignItems: 'center',
    borderBottomColor: colors.mineral,
    borderBottomWidth: StyleSheet.hairlineWidth,
    flexDirection: 'row',
    gap: spacing.md,
    justifyContent: 'space-between',
    padding: spacing.md,
  },
  headingCopy: { flex: 1 },
  title: { ...typography.title, color: colors.ink },
  count: { ...typography.caption, color: colors.inkMuted },
  close: {
    alignItems: 'center',
    borderColor: colors.mineral,
    borderRadius: radii.pill,
    borderWidth: 1,
    justifyContent: 'center',
    minHeight: layout.minimumTouchTarget,
    minWidth: 72,
    paddingHorizontal: spacing.sm,
  },
  closeText: { ...typography.label, color: colors.cyan },
  pressed: { opacity: 0.72 },
  list: { gap: spacing.sm, padding: spacing.md, paddingBottom: spacing.xxxl },
  empty: { ...typography.body, color: colors.inkSecondary },
  row: {
    backgroundColor: colors.graphiteRaised,
    borderColor: colors.mineral,
    borderRadius: radii.md,
    borderWidth: 1,
    gap: spacing.xs,
    padding: spacing.sm,
  },
  rowLabel: { ...typography.label, color: colors.ink },
  cells: { flexDirection: 'row', flexWrap: 'wrap', gap: spacing.sm },
  cell: { flexGrow: 1, minWidth: 72 },
  cellLabel: { ...typography.micro, color: colors.inkMuted },
  cellValue: { ...typography.caption, color: colors.inkSecondary },
});
