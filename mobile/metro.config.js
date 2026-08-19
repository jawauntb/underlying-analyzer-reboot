const { getDefaultConfig } = require('expo/metro-config');

const {
  isRuntimeFetchRequest,
  selectRuntimeFetchModule,
} = require('./runtime-fetch.config');

const config = getDefaultConfig(__dirname);
const defaultResolveRequest = config.resolver.resolveRequest;
const runtimeFetchModule = selectRuntimeFetchModule(process.env.EXPO_PUBLIC_E2E_MODE);

config.resolver.resolveRequest = (context, moduleName, platform) => {
  if (isRuntimeFetchRequest(context, moduleName)) {
    return context.resolveRequest(context, runtimeFetchModule, platform);
  }
  if (typeof defaultResolveRequest === 'function') {
    return defaultResolveRequest(context, moduleName, platform);
  }
  return context.resolveRequest(context, moduleName, platform);
};

module.exports = config;
