# Undercurrent mobile

Undercurrent is the native Expo Go companion to The Underlying Analyzer. It is guest-first, read-only market research for iPhone: Pulse, saved Lists, a focused Ticker Lens, and an explicitly started Research Run.

Research Run asks for and server-enforces `ticker_research_bundle` as the first
tool call, refusing any other tool before it can run:
one bounded, Massive-first packet of the ticker's 1M, 3M, and 1Y Auction,
Regression, Ridge, Compass, Torque, portfolio, and volatility data, plus
seasonality and options. The streamed agent result uses the packet's compact
decision context; direct HTTP or MCP callers can obtain the full datasets. This
is user-triggered and is intentionally outside the low-call
production smoke check.

## Run in Expo Go

Use Node 22.16.0, then install the locked dependencies and start Metro.

```bash
nvm use
npm ci
npm start
```

Scan the QR code with the ordinary App Store Expo Go app. The checked-in default points at the public production API. To use another server, set `EXPO_PUBLIC_API_BASE_URL` to a credential-free HTTPS origin before starting Expo.

## Local verification

```bash
npm run lint
npm run typecheck
npm run test:ci
npm run test:scripts
npm run check:expo
npm run doctor
npm run export:web
npm run export:ios
npm run scan:source
npm run scan:exports
```

`npm run smoke:production` makes exactly one request per approved live check: health, the agent tool catalog, watchlist alerts, Auction data, and Torque data. It never calls the agent, retries zero times, and writes a metadata-only receipt under `.artifacts/receipts/`. Failures are labeled as contract drift, service outage, mixed failure, or verifier error; response payloads are never persisted. Contract drift and verifier faults fail CI, while a pure external outage becomes a visible workflow warning with its receipt preserved.

The scanners reject recognized credentials, private signing files, symlinks, and oversized artifacts. They persist only counts, policy identifiers, and a content digest. Generated exports and receipts live in `.artifacts/`, which is ignored by Git. A failed export scan uploads its safe receipt but never uploads the rejected bundle.

## Cloud proof

The `Mobile cloud proof` GitHub Actions workflow runs four bounded jobs:

- backend agent contract: the Python test suite plus focused lint/type safety for the selected-tool boundary;
- quality: source scan, ESLint, TypeScript, Jest, Node policy tests, Expo dependency check, and Expo Doctor;
- static export: web and iOS JavaScript/static bundle exports followed by an artifact scan;
- production contract: the five read-only or data-only API checks described above.

The iOS export proves Metro can produce the iOS bundle on a Linux runner. It is not a signed native build and does not replace a real iPhone Expo Go pass.

The app is linked to [`@jawauntb/undercurrent`](https://expo.dev/accounts/jawauntb/projects/undercurrent). `eas.json` provides an unsigned `e2e-test` iOS simulator profile for native test automation and a signed `production` profile for App Store Connect. Production builds use EAS-managed build numbers so every TestFlight upload receives a unique version.

## TestFlight release

Green `Mobile cloud proof` on `main` runs `.github/workflows/ios-eas-production.yml`, waits on EAS, and auto-submits to App Store Connect. Manual retry or a version tag:

```bash
gh workflow run ios-eas-production.yml --ref main
git tag v0.1.0 && git push origin v0.1.0
```

Requires repo secret `EXPO_TOKEN`. Do not set `EAS_NO_VCS=1`. Local equivalent:

```bash
npx eas-cli build --platform ios --profile production --auto-submit --non-interactive
```

The first release may prompt an Apple Developer account holder to create or select the distribution certificate, provisioning profile, and App Store Connect app record. Never commit downloaded signing credentials or App Store Connect API keys.
