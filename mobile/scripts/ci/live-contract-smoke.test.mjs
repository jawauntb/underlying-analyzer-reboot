import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import { runLiveContractSmoke } from './live-contract-smoke.mjs';

const BASE_URL = 'https://api.example.test';

const successPayloads = Object.freeze({
  '/api/health': { ok: true, service: 'underlying-terminal' },
  '/api/agent/tools': {
    ok: true,
    tools: [
      { name: 'analyze_ticker', http: { method: 'GET', path: '/api/analysis/{ticker}' } },
      { name: 'stock_fax', http: { method: 'POST', path: '/api/tools/fax' } },
      { name: 'sec_source_pack', http: { method: 'GET', path: '/api/sec/{ticker}' } },
      { name: 'search_news', http: { method: 'POST', path: '/api/news' } },
      { name: 'chart_data', http: { method: 'POST', path: '/api/data/charts/{chart_type}' } },
      { name: 'provider_status', http: { method: 'GET', path: '/api/providers' } },
    ],
  },
  '/api/watchlists/alerts': {
    rows: [],
    alerts: [],
    digest: {},
    meta: {},
    export: { mode: 'watchlist-alerts' },
  },
  '/api/data/charts/auction': {
    datasets: [
      { chart_type: 'auction', ticker: 'AAPL', levels: {}, series: {} },
    ],
    export: { mode: 'auction-data' },
  },
  '/api/data/tools/torque': {
    chart_type: 'torque',
    ticker: 'AAPL',
    torque: {},
    meta: {},
    series: {},
    export: { mode: 'torque-data' },
  },
});

function jsonResponse(payload, init = {}) {
  return new Response(JSON.stringify(payload), {
    status: init.status ?? 200,
    headers: { 'Content-Type': 'application/json', ...(init.headers ?? {}) },
  });
}

function successfulFetch(overrides = {}) {
  return async (url) => {
    const path = new URL(url).pathname;
    const override = overrides[path];
    if (override instanceof Error) throw override;
    if (override) return override;
    return jsonResponse(successPayloads[path]);
  };
}

async function runWithTemporaryReceipt(t, fetchImpl) {
  const directory = await mkdtemp(join(tmpdir(), 'undercurrent-live-smoke-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const receiptPath = join(directory, 'receipt.json');
  const result = await runLiveContractSmoke({ baseUrl: BASE_URL, receiptPath, fetchImpl });
  return { ...result, serialized: await readFile(receiptPath, 'utf8') };
}

test('live smoke accepts representative contracts and writes metadata-only evidence', async (t) => {
  const requests = [];
  const fetchImpl = async (url, init) => {
    requests.push({ path: new URL(url).pathname, init });
    return successfulFetch()(url);
  };

  const { receipt, serialized } = await runWithTemporaryReceipt(t, fetchImpl);

  assert.equal(receipt.conclusion, 'passed');
  assert.equal(receipt.checks.length, 5);
  assert.ok(receipt.checks.every(({ status }) => status === 'passed'));
  assert.equal(requests.length, 5);
  assert.ok(requests.every(({ init }) => init.redirect === 'error'));
  assert.doesNotMatch(serialized, /watchlist-alerts.*rows/s);
  assert.match(serialized, /"kind": "undercurrent-production-contract-smoke"/);
});

test('live smoke classifies invalid content and response shape as contract drift', async (t) => {
  const { receipt } = await runWithTemporaryReceipt(
    t,
    successfulFetch({
      '/api/health': new Response('healthy', {
        headers: { 'Content-Type': 'text/plain' },
      }),
      '/api/data/tools/torque': jsonResponse({ chart_type: 'torque', ticker: 'AAPL' }),
    }),
  );

  assert.equal(receipt.conclusion, 'contract-drift');
  assert.deepEqual(receipt.failureClasses, ['contract-drift']);
  assert.equal(receipt.checks.filter(({ status }) => status === 'failed').length, 2);
});

test('live smoke distinguishes a pure outage from mixed failures', async (t) => {
  const outage = await runWithTemporaryReceipt(
    t,
    async () => jsonResponse({ unavailable: true }, { status: 503 }),
  );
  assert.equal(outage.receipt.conclusion, 'service-outage');
  assert.deepEqual(outage.receipt.failureClasses, ['service-outage']);

  const mixed = await runWithTemporaryReceipt(
    t,
    successfulFetch({
      '/api/health': jsonResponse({ unavailable: true }, { status: 503 }),
      '/api/agent/tools': jsonResponse({ ok: true, tools: [] }),
    }),
  );
  assert.equal(mixed.receipt.conclusion, 'mixed-failure');
  assert.deepEqual(mixed.receipt.failureClasses, ['contract-drift', 'service-outage']);
});

test('live smoke classifies transport timeouts without leaking response payloads', async (t) => {
  const timeout = new Error('request timed out');
  timeout.name = 'TimeoutError';
  const { receipt, serialized } = await runWithTemporaryReceipt(t, async () => {
    throw timeout;
  });

  assert.equal(receipt.conclusion, 'service-outage');
  assert.ok(receipt.checks.every(({ errorCode }) => errorCode === 'TimeoutError'));
  assert.doesNotMatch(serialized, /request timed out/);
});
