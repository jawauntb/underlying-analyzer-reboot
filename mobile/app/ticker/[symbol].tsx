import { useLocalSearchParams } from 'expo-router';

import LensScreen from '@/src/features/lens/LensScreen';

export function generateStaticParams() {
  return [{ symbol: 'AAPL' }];
}

function firstParam(value: string | string[] | undefined) {
  return Array.isArray(value) ? value[0] : value;
}

export default function TickerScreen() {
  const params = useLocalSearchParams<{ symbol?: string | string[] }>();
  return <LensScreen symbol={firstParam(params.symbol) ?? 'AAPL'} />;
}
