export const colors = {
  graphite: '#171816',
  graphiteRaised: '#22231F',
  graphiteSoft: '#31332E',
  mineral: '#45483F',
  mineralSoft: '#2B2D28',
  ink: '#F4F0E7',
  inkSecondary: '#C7C3B8',
  inkMuted: '#918F87',
  mint: '#92E6B4',
  coral: '#FF8C74',
  cyan: '#79D5DE',
} as const;

export const chartColors = {
  surface: colors.graphiteRaised,
  plot: colors.graphiteSoft,
  grid: colors.mineral,
  primary: colors.ink,
  positive: colors.mint,
  negative: colors.coral,
  secondary: colors.cyan,
  muted: colors.inkMuted,
} as const;

export const spacing = {
  xs: 6,
  sm: 10,
  md: 16,
  lg: 22,
  xl: 30,
  xxl: 40,
  xxxl: 56,
} as const;

export const radii = {
  md: 12,
  lg: 16,
  xl: 24,
  pill: 999,
} as const;

export const layout = {
  minimumTouchTarget: 44,
  maximumContentWidth: 680,
} as const;

export const typography = {
  display: {
    fontSize: 42,
    fontWeight: '700' as const,
    letterSpacing: -1.3,
    lineHeight: 48,
  },
  headline: {
    fontSize: 22,
    fontWeight: '700' as const,
    letterSpacing: -0.35,
    lineHeight: 28,
  },
  title: {
    fontSize: 20,
    fontWeight: '600' as const,
    letterSpacing: -0.2,
    lineHeight: 26,
  },
  body: {
    fontSize: 17,
    fontWeight: '400' as const,
    lineHeight: 25,
  },
  label: {
    fontSize: 16,
    fontWeight: '600' as const,
    lineHeight: 21,
  },
  caption: {
    fontSize: 14,
    fontWeight: '500' as const,
    lineHeight: 20,
  },
  eyebrow: {
    fontSize: 12,
    fontWeight: '700' as const,
    letterSpacing: 1.2,
    lineHeight: 16,
  },
  micro: {
    fontSize: 11,
    fontWeight: '700' as const,
    letterSpacing: 0.9,
    lineHeight: 15,
  },
} as const;
