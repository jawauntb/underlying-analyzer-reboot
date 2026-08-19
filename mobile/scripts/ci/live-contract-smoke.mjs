import { mkdir, writeFile } from 'node:fs/promises';
import { dirname } from 'node:path';
import { pathToFileURL } from 'node:url';

import {
  classifyFailure,
  ContractDriftError,
  exitCodeForConclusion,
  validateAlerts,
  validateAuction,
  validateHealth,
  validateTools,
  validateTorque,
} from './contract-policy.mjs';

const DEFAULT_BASE_URL = 'https://underlying-terminal-production.up.railway.app';
const DEFAULT_RECEIPT = '.artifacts/receipts/live-contract-smoke.json';
const MAX_RESPONSE_BYTES = 10 * 1024 * 1024;
const CAPABILITY_TIMEOUT_MS = 10_000;
const MARKET_TIMEOUT_MS = 30_000;

class HttpStatusError extends Error {
  constructor(status) {
    super(`Unexpected HTTP status ${status}.`);
    this.name = 'HttpStatusError';
    this.status = status;
  }
}

async function readJsonBounded(response) {
  const declaredLength = Number(response.headers.get('content-length') ?? 0);
  if (declaredLength > MAX_RESPONSE_BYTES) {
    throw new ContractDriftError('response.too_large', 'Response exceeded the bounded smoke-test limit.');
  }

  if (!response.body) {
    throw new ContractDriftError('response.missing_body', 'Response did not include a body.');
  }

  const reader = response.body.getReader();
  const chunks = [];
  let byteLength = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    byteLength += value.byteLength;
    if (byteLength > MAX_RESPONSE_BYTES) {
      await reader.cancel();
      throw new ContractDriftError('response.too_large', 'Response exceeded the bounded smoke-test limit.');
    }
    chunks.push(value);
  }

  const bytes = new Uint8Array(byteLength);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  const text = new TextDecoder().decode(bytes);
  try {
    return { payload: JSON.parse(text), byteLength };
  } catch {
    throw new ContractDriftError('response.invalid_json', 'Response was not valid JSON.');
  }
}

const CHECKS = [
  {
    name: 'health',
    method: 'GET',
    path: '/api/health',
    timeoutMs: CAPABILITY_TIMEOUT_MS,
    validate: validateHealth,
  },
  {
    name: 'tools',
    method: 'GET',
    path: '/api/agent/tools',
    timeoutMs: CAPABILITY_TIMEOUT_MS,
    validate: validateTools,
  },
  {
    name: 'alerts',
    method: 'POST',
    path: '/api/watchlists/alerts',
    timeoutMs: MARKET_TIMEOUT_MS,
    body: { tickers: ['AAPL'], max_results: 1, max_alerts: 3, period: '5d' },
    validate: validateAlerts,
  },
  {
    name: 'auction',
    method: 'POST',
    path: '/api/data/charts/auction',
    timeoutMs: MARKET_TIMEOUT_MS,
    body: { ticker: 'AAPL', period: '5d', max_results: 1 },
    validate: validateAuction,
  },
  {
    name: 'torque',
    method: 'POST',
    path: '/api/data/tools/torque',
    timeoutMs: MARKET_TIMEOUT_MS,
    body: { ticker: 'AAPL' },
    validate: validateTorque,
  },
];

function safeBaseUrl(raw) {
  const parsed = new URL(raw);
  if (
    parsed.protocol !== 'https:' ||
    parsed.username ||
    parsed.password ||
    parsed.pathname !== '/' ||
    parsed.search ||
    parsed.hash
  ) {
    throw new Error('CONTRACT_SMOKE_BASE_URL must be a credential-free HTTPS origin.');
  }
  return parsed.toString().replace(/\/$/, '');
}

async function executeCheck(baseUrl, check, fetchImpl) {
  const started = performance.now();
  let httpStatus = null;
  let responseBytes = null;
  let contentType = null;

  try {
    const response = await fetchImpl(`${baseUrl}${check.path}`, {
      method: check.method,
      body: check.body ? JSON.stringify(check.body) : undefined,
      headers: {
        Accept: 'application/json',
        ...(check.body ? { 'Content-Type': 'application/json' } : {}),
        'User-Agent': 'undercurrent-mobile-contract-smoke/1.0',
      },
      redirect: 'error',
      signal: AbortSignal.timeout(check.timeoutMs),
    });
    httpStatus = response.status;
    contentType = response.headers.get('content-type');
    if (!response.ok) {
      await response.body?.cancel();
      throw new HttpStatusError(response.status);
    }
    if (!contentType?.toLowerCase().includes('application/json')) {
      await response.body?.cancel();
      throw new ContractDriftError('response.content_type', 'Response content type must be JSON.');
    }
    const parsed = await readJsonBounded(response);
    responseBytes = parsed.byteLength;
    const assertions = check.validate(parsed.payload);
    return {
      name: check.name,
      method: check.method,
      path: check.path,
      status: 'passed',
      classification: null,
      httpStatus,
      durationMs: Math.round(performance.now() - started),
      responseBytes,
      contentType,
      attempts: 1,
      retries: 0,
      assertions: ['content-type:json', ...assertions],
    };
  } catch (error) {
    return {
      name: check.name,
      method: check.method,
      path: check.path,
      status: 'failed',
      classification: classifyFailure(error, check.name),
      errorCode: error?.code ?? error?.name ?? 'unknown',
      httpStatus,
      durationMs: Math.round(performance.now() - started),
      responseBytes,
      contentType,
      attempts: 1,
      retries: 0,
      assertions: [],
    };
  }
}

function conclusionFor(checks) {
  const classes = [...new Set(checks.map((check) => check.classification).filter(Boolean))];
  if (classes.length === 0) return { conclusion: 'passed', failureClasses: [] };
  if (classes.length === 1) return { conclusion: classes[0], failureClasses: classes };
  return { conclusion: 'mixed-failure', failureClasses: classes.sort() };
}

export async function runLiveContractSmoke({
  baseUrl = process.env.CONTRACT_SMOKE_BASE_URL ?? DEFAULT_BASE_URL,
  receiptPath = process.env.CONTRACT_SMOKE_RECEIPT ?? DEFAULT_RECEIPT,
  fetchImpl = fetch,
} = {}) {
  const normalizedBaseUrl = safeBaseUrl(baseUrl);
  const startedAt = new Date().toISOString();
  const checks = [];

  for (const check of CHECKS) {
    checks.push(await executeCheck(normalizedBaseUrl, check, fetchImpl));
  }

  const completedAt = new Date().toISOString();
  const { conclusion, failureClasses } = conclusionFor(checks);
  const receipt = {
    schemaVersion: 1,
    kind: 'undercurrent-production-contract-smoke',
    conclusion,
    failureClasses,
    startedAt,
    completedAt,
    baseOrigin: new URL(normalizedBaseUrl).origin,
    retryPolicy: { retries: 0, attemptsPerCheck: 1 },
    limits: {
      capabilityTimeoutMs: CAPABILITY_TIMEOUT_MS,
      marketTimeoutMs: MARKET_TIMEOUT_MS,
      maxResponseBytes: MAX_RESPONSE_BYTES,
    },
    runtime: {
      node: process.version,
      ci: process.env.CI === 'true',
      gitSha: process.env.GITHUB_SHA ?? null,
      runId: process.env.GITHUB_RUN_ID ?? null,
    },
    checks,
  };

  await mkdir(dirname(receiptPath), { recursive: true });
  await writeFile(receiptPath, `${JSON.stringify(receipt, null, 2)}\n`, { mode: 0o600 });
  return { receipt, receiptPath };
}

async function main() {
  const { receipt, receiptPath } = await runLiveContractSmoke();
  for (const check of receipt.checks) {
    const label = check.status === 'passed' ? 'PASS' : check.classification.toUpperCase();
    console.log(`${label} ${check.method} ${check.path} (${check.durationMs}ms)`);
  }
  console.log(`Metadata-only receipt: ${receiptPath}`);

  if (receipt.conclusion !== 'passed') {
    const title = receipt.failureClasses.includes('contract-drift')
      ? 'Production API contract drift'
      : receipt.failureClasses.includes('verification-error')
        ? 'Production API verifier error'
        : 'Production API service outage';
    const annotation = receipt.conclusion === 'service-outage' ? 'warning' : 'error';
    console.error(`::${annotation} title=${title}::Smoke conclusion: ${receipt.conclusion}`);
  }
  process.exitCode = exitCodeForConclusion(receipt.conclusion, receipt.failureClasses);
}

const entryPath = process.argv[1] ? pathToFileURL(process.argv[1]).href : null;
if (entryPath === import.meta.url) {
  await main();
}
