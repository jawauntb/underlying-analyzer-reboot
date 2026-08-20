import { DarkTheme, ThemeProvider } from '@react-navigation/native';
import { Stack } from 'expo-router';
import { StatusBar } from 'expo-status-bar';
import { useEffect, useState } from 'react';
import { AccessibilityInfo } from 'react-native';
import { SafeAreaProvider } from 'react-native-safe-area-context';

import { colors } from '@/src/theme/tokens';
import { SavedListsProvider } from '@/src/features/lists/watchlists';
import { NetworkProvider } from '@/src/state/network';
import { PreferencesProvider } from '@/src/state/preferences';
import E2EFixtureBadge from '@/src/testing/E2EFixtureBadge';

export const unstable_settings = {
  initialRouteName: '(tabs)',
  anchor: '(tabs)',
};

export function stackAnimationFor(reduceMotion: boolean) {
  return reduceMotion ? ('none' as const) : ('default' as const);
}

export const tickerScreenOptions = {
  headerBackTitle: 'Back',
  title: 'Ticker Lens',
} as const;

const navigationTheme = {
  ...DarkTheme,
  colors: {
    ...DarkTheme.colors,
    primary: colors.mint,
    background: colors.graphite,
    card: colors.graphiteRaised,
    text: colors.ink,
    border: colors.mineral,
    notification: colors.coral,
  },
};

function useReduceMotionPreference() {
  const [reduceMotion, setReduceMotion] = useState(true);

  useEffect(() => {
    let mounted = true;
    const subscription = AccessibilityInfo.addEventListener('reduceMotionChanged', setReduceMotion);

    void AccessibilityInfo.isReduceMotionEnabled().then((enabled) => {
      if (mounted) {
        setReduceMotion(enabled);
      }
    });

    return () => {
      mounted = false;
      subscription.remove();
    };
  }, []);

  return reduceMotion;
}

export default function RootLayout() {
  const reduceMotion = useReduceMotionPreference();

  return (
    <SafeAreaProvider>
      <NetworkProvider>
        <PreferencesProvider>
          <SavedListsProvider>
            <ThemeProvider value={navigationTheme}>
              <Stack
                screenOptions={{
                  animation: stackAnimationFor(reduceMotion),
                  contentStyle: { backgroundColor: colors.graphite },
                  headerBackButtonDisplayMode: 'minimal',
                  headerShadowVisible: false,
                  headerStyle: { backgroundColor: colors.graphiteRaised },
                  headerTintColor: colors.ink,
                  headerTitleStyle: { fontWeight: '700' },
                }}>
                <Stack.Screen name="(tabs)" options={{ headerShown: false }} />
                <Stack.Screen
                  name="ticker/[symbol]"
                  options={tickerScreenOptions}
                />
                <Stack.Screen
                  name="research"
                  options={{
                    presentation: 'formSheet',
                    sheetAllowedDetents: [0.62, 0.92],
                    sheetGrabberVisible: true,
                    title: 'Research Run',
                  }}
                />
              </Stack>
              <StatusBar animated={!reduceMotion} style="light" />
              <E2EFixtureBadge />
            </ThemeProvider>
          </SavedListsProvider>
        </PreferencesProvider>
      </NetworkProvider>
    </SafeAreaProvider>
  );
}
