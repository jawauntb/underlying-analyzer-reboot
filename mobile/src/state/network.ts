import NetInfo, { type NetInfoState } from '@react-native-community/netinfo';
import { createContext, createElement, type PropsWithChildren, useContext, useEffect, useState } from 'react';

export type NetworkReachability = 'online' | 'offline' | 'unknown';

export function reachabilityFromNetInfo(state: NetInfoState): NetworkReachability {
  if (state.isConnected === false || state.isInternetReachable === false) return 'offline';
  if (state.isConnected === true) return 'online';
  return 'unknown';
}

const NetworkContext = createContext<NetworkReachability>('unknown');

export function NetworkProvider({ children }: PropsWithChildren): ReturnType<typeof createElement> {
  const [reachability, setReachability] = useState<NetworkReachability>('unknown');
  useEffect(() => NetInfo.addEventListener((state) => setReachability(reachabilityFromNetInfo(state))), []);
  return createElement(NetworkContext.Provider, { value: reachability }, children);
}

export function useNetworkReachability(): NetworkReachability {
  return useContext(NetworkContext);
}
