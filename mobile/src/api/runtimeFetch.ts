import { fetch as expoFetch } from 'expo/fetch';

export type RuntimeFetch = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

export const runtimeFetch = expoFetch as unknown as RuntimeFetch;
