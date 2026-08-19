import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import test from 'node:test';

const require = createRequire(import.meta.url);
const {
  CLIENT_MODULE_PATH,
  E2E_RUNTIME_FETCH_PATH,
  PRODUCTION_RUNTIME_FETCH_PATH,
  isRuntimeFetchRequest,
  selectRuntimeFetchModule,
} = require('../../runtime-fetch.config.js');

test('runtime fetch selection enables fixtures only for the exact E2E build flag', () => {
  assert.equal(selectRuntimeFetchModule(undefined), PRODUCTION_RUNTIME_FETCH_PATH);
  assert.equal(selectRuntimeFetchModule('true'), PRODUCTION_RUNTIME_FETCH_PATH);
  assert.equal(selectRuntimeFetchModule('1'), E2E_RUNTIME_FETCH_PATH);
});

test('runtime fetch redirection is scoped to the API client import', () => {
  assert.equal(isRuntimeFetchRequest({ originModulePath: CLIENT_MODULE_PATH }, './runtimeFetch'), true);
  assert.equal(isRuntimeFetchRequest({ originModulePath: CLIENT_MODULE_PATH }, './otherModule'), false);
  assert.equal(isRuntimeFetchRequest({ originModulePath: E2E_RUNTIME_FETCH_PATH }, './runtimeFetch'), false);
});

test('production runtime fetch sources do not import deterministic fixture payloads', async () => {
  const [clientSource, productionSource, e2eSource] = await Promise.all([
    readFile(CLIENT_MODULE_PATH, 'utf8'),
    readFile(PRODUCTION_RUNTIME_FETCH_PATH, 'utf8'),
    readFile(E2E_RUNTIME_FETCH_PATH, 'utf8'),
  ]);

  assert.match(clientSource, /from '\.\/runtimeFetch'/);
  assert.doesNotMatch(clientSource, /testing\/e2eFetch|EXPO_PUBLIC_E2E_MODE/);
  assert.doesNotMatch(productionSource, /testing\/e2eFetch/);
  assert.match(e2eSource, /testing\/e2eFetch/);
});
