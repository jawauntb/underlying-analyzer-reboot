import assert from 'node:assert/strict';
import test from 'node:test';

import {
  ContractDriftError,
  EXPECTED_MOBILE_TOOLS,
  classifyFailure,
  exitCodeForConclusion,
  validateTools,
} from './contract-policy.mjs';

const tool = (name, method, path) => ({ name, http: { method, path } });

test('the tool contract accepts the exact native research capability set', () => {
  const payload = {
    ok: true,
    tools: [
      tool('analyze_ticker', 'GET', '/api/analysis/{ticker}'),
      tool('stock_fax', 'POST', '/api/tools/fax'),
      tool('sec_source_pack', 'GET', '/api/sec/{ticker}'),
      tool('search_news', 'POST', '/api/news'),
      tool('chart_data', 'POST', '/api/data/charts/{chart_type}'),
      tool('provider_status', 'GET', '/api/providers'),
    ],
  };

  assert.doesNotThrow(() => validateTools(payload));
  assert.equal(EXPECTED_MOBILE_TOOLS.length, 6);
});

test('the tool contract rejects path drift', () => {
  const payload = {
    ok: true,
    tools: EXPECTED_MOBILE_TOOLS.map((name) =>
      tool(name, name === 'analyze_ticker' ? 'GET' : 'POST', `/wrong/${name}`),
    ),
  };

  assert.throws(() => validateTools(payload), ContractDriftError);
});

test('failure classification separates drift, provider outage, and verifier faults', () => {
  assert.equal(classifyFailure(new ContractDriftError('shape', 'shape changed'), 'tools'), 'contract-drift');
  assert.equal(classifyFailure({ name: 'HttpStatusError', status: 404 }, 'tools'), 'contract-drift');
  assert.equal(classifyFailure({ name: 'HttpStatusError', status: 400 }, 'auction'), 'contract-drift');
  assert.equal(classifyFailure({ name: 'HttpStatusError', status: 503 }, 'health'), 'service-outage');
  assert.equal(classifyFailure({ name: 'TimeoutError' }, 'health'), 'service-outage');
  assert.equal(classifyFailure(new Error('bug'), 'health'), 'verification-error');
});

test('only a pure external outage receives the nonblocking exit code', () => {
  assert.equal(exitCodeForConclusion('passed', []), 0);
  assert.equal(exitCodeForConclusion('service-outage', ['service-outage']), 2);
  assert.equal(exitCodeForConclusion('contract-drift', ['contract-drift']), 1);
  assert.equal(exitCodeForConclusion('mixed-failure', ['contract-drift', 'service-outage']), 1);
  assert.equal(exitCodeForConclusion('verification-error', ['verification-error']), 1);
});
