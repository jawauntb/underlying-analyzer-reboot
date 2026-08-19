import Ionicons from '@expo/vector-icons/Ionicons';
import * as Haptics from 'expo-haptics';
import { Tabs } from 'expo-router';

import { colors, layout } from '@/src/theme/tokens';

export const unstable_settings = {
  initialRouteName: 'index',
};

function acknowledgeTabPress() {
  void Haptics.selectionAsync().catch(() => undefined);
}

export default function TabLayout() {
  return (
    <Tabs
      screenListeners={{ tabPress: acknowledgeTabPress }}
      screenOptions={{
        headerShown: false,
        sceneStyle: { backgroundColor: colors.graphite },
        tabBarActiveTintColor: colors.mint,
        tabBarInactiveTintColor: colors.inkMuted,
        tabBarAllowFontScaling: true,
        tabBarHideOnKeyboard: true,
        tabBarItemStyle: { minHeight: layout.minimumTouchTarget },
        tabBarLabelStyle: { fontSize: 12, fontWeight: '600', letterSpacing: 0.2 },
        tabBarStyle: {
          backgroundColor: colors.graphiteRaised,
          borderTopColor: colors.mineral,
          minHeight: 64,
        },
      }}>
      <Tabs.Screen
        name="index"
        options={{
          title: 'Pulse',
          tabBarAccessibilityLabel: 'Pulse tab',
          tabBarIcon: ({ color, focused, size }) => (
            <Ionicons color={color} name={focused ? 'pulse' : 'pulse-outline'} size={size} />
          ),
        }}
      />
      <Tabs.Screen
        name="search"
        options={{
          title: 'Search',
          tabBarAccessibilityLabel: 'Search tab',
          tabBarIcon: ({ color, focused, size }) => (
            <Ionicons color={color} name={focused ? 'search' : 'search-outline'} size={size} />
          ),
        }}
      />
      <Tabs.Screen
        name="lists"
        options={{
          title: 'Lists',
          tabBarAccessibilityLabel: 'Lists tab',
          tabBarIcon: ({ color, focused, size }) => (
            <Ionicons color={color} name={focused ? 'list' : 'list-outline'} size={size} />
          ),
        }}
      />
      <Tabs.Screen
        name="library"
        options={{
          title: 'Library',
          tabBarAccessibilityLabel: 'Library tab',
          tabBarIcon: ({ color, focused, size }) => (
            <Ionicons color={color} name={focused ? 'library' : 'library-outline'} size={size} />
          ),
        }}
      />
    </Tabs>
  );
}
