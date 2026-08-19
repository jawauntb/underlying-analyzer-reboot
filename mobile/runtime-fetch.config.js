/* global __dirname */

const path = require('node:path');

const CLIENT_MODULE_PATH = path.join(__dirname, 'src', 'api', 'client.ts');
const PRODUCTION_RUNTIME_FETCH_PATH = path.join(__dirname, 'src', 'api', 'runtimeFetch.ts');
const E2E_RUNTIME_FETCH_PATH = path.join(__dirname, 'src', 'api', 'runtimeFetch.e2e.ts');
const RUNTIME_FETCH_REQUEST = './runtimeFetch';

function isRuntimeFetchRequest(context, moduleName) {
  return moduleName === RUNTIME_FETCH_REQUEST
    && path.resolve(context.originModulePath) === CLIENT_MODULE_PATH;
}

function selectRuntimeFetchModule(e2eMode) {
  return e2eMode === '1' ? E2E_RUNTIME_FETCH_PATH : PRODUCTION_RUNTIME_FETCH_PATH;
}

module.exports = {
  CLIENT_MODULE_PATH,
  E2E_RUNTIME_FETCH_PATH,
  PRODUCTION_RUNTIME_FETCH_PATH,
  isRuntimeFetchRequest,
  selectRuntimeFetchModule,
};
