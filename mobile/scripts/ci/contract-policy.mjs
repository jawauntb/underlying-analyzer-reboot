const EXPECTED_TOOL_ENTRIES = Object.freeze({
  analyze_ticker: { method: 'GET', path: '/api/analysis/{ticker}' },
  stock_fax: { method: 'POST', path: '/api/tools/fax' },
  sec_source_pack: { method: 'GET', path: '/api/sec/{ticker}' },
  search_news: { method: 'POST', path: '/api/news' },
  chart_data: { method: 'POST', path: '/api/data/charts/{chart_type}' },
  provider_status: { method: 'GET', path: '/api/providers' },
});

export const EXPECTED_MOBILE_TOOLS = Object.freeze(Object.keys(EXPECTED_TOOL_ENTRIES));

export class ContractDriftError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'ContractDriftError';
    this.code = code;
  }
}

function isRecord(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function drift(condition, code, message) {
  if (!condition) {
    throw new ContractDriftError(code, message);
  }
}

export function validateHealth(payload) {
  drift(isRecord(payload), 'health.object', 'Health response must be an object.');
  drift(payload.ok === true, 'health.ok', 'Health response must report ok=true.');
  drift(
    typeof payload.service === 'string' && payload.service.length > 0,
    'health.service',
    'Health response must name the service.',
  );
  return ['object', 'ok', 'service'];
}

export function validateTools(payload) {
  drift(isRecord(payload), 'tools.object', 'Tool catalog must be an object.');
  drift(payload.ok === true, 'tools.ok', 'Tool catalog must report ok=true.');
  drift(Array.isArray(payload.tools), 'tools.array', 'Tool catalog must include a tools array.');

  const toolsByName = new Map(
    payload.tools
      .filter(isRecord)
      .filter((tool) => typeof tool.name === 'string')
      .map((tool) => [tool.name, tool]),
  );

  for (const [name, expected] of Object.entries(EXPECTED_TOOL_ENTRIES)) {
    const tool = toolsByName.get(name);
    drift(isRecord(tool), `tools.missing.${name}`, `Tool catalog is missing ${name}.`);
    drift(isRecord(tool.http), `tools.http.${name}`, `${name} must include its HTTP contract.`);
    drift(tool.http.method === expected.method, `tools.method.${name}`, `${name} HTTP method drifted.`);
    drift(tool.http.path === expected.path, `tools.path.${name}`, `${name} HTTP path drifted.`);
  }

  return ['object', 'ok', 'tools', ...EXPECTED_MOBILE_TOOLS.map((name) => `tool:${name}`)];
}

export function validateAlerts(payload) {
  drift(isRecord(payload), 'alerts.object', 'Alerts response must be an object.');
  drift(Array.isArray(payload.rows), 'alerts.rows', 'Alerts response must include rows.');
  drift(Array.isArray(payload.alerts), 'alerts.alerts', 'Alerts response must include alerts.');
  drift(isRecord(payload.digest), 'alerts.digest', 'Alerts response must include a digest.');
  drift(isRecord(payload.meta), 'alerts.meta', 'Alerts response must include metadata.');
  drift(isRecord(payload.export), 'alerts.export', 'Alerts response must include export metadata.');
  drift(payload.export.mode === 'watchlist-alerts', 'alerts.mode', 'Alerts export mode drifted.');
  return ['object', 'rows', 'alerts', 'digest', 'meta', 'export:watchlist-alerts'];
}

export function validateAuction(payload) {
  drift(isRecord(payload), 'auction.object', 'Auction response must be an object.');
  drift(Array.isArray(payload.datasets), 'auction.datasets', 'Auction response must include datasets.');
  drift(payload.datasets.length > 0, 'auction.nonempty', 'Auction response must include one dataset.');
  const dataset = payload.datasets[0];
  drift(isRecord(dataset), 'auction.dataset', 'Auction dataset must be an object.');
  drift(dataset.chart_type === 'auction', 'auction.type', 'Auction chart type drifted.');
  drift(dataset.ticker === 'AAPL', 'auction.ticker', 'Auction response ticker drifted.');
  drift(isRecord(dataset.levels), 'auction.levels', 'Auction dataset must include levels.');
  drift(isRecord(dataset.series), 'auction.series', 'Auction dataset must include series.');
  drift(isRecord(payload.export), 'auction.export', 'Auction response must include export metadata.');
  drift(payload.export.mode === 'auction-data', 'auction.mode', 'Auction export mode drifted.');
  return ['object', 'datasets', 'chart_type:auction', 'ticker:AAPL', 'levels', 'series', 'export:auction-data'];
}

export function validateTorque(payload) {
  drift(isRecord(payload), 'torque.object', 'Torque response must be an object.');
  drift(payload.chart_type === 'torque', 'torque.type', 'Torque chart type drifted.');
  drift(payload.ticker === 'AAPL', 'torque.ticker', 'Torque response ticker drifted.');
  drift(isRecord(payload.torque), 'torque.score', 'Torque response must include its score pack.');
  drift(isRecord(payload.meta), 'torque.meta', 'Torque response must include metadata.');
  drift(isRecord(payload.series), 'torque.series', 'Torque response must include chart series.');
  drift(isRecord(payload.export), 'torque.export', 'Torque response must include export metadata.');
  drift(payload.export.mode === 'torque-data', 'torque.mode', 'Torque export mode drifted.');
  return ['object', 'chart_type:torque', 'ticker:AAPL', 'torque', 'meta', 'series', 'export:torque-data'];
}

export function classifyFailure(error, checkName) {
  if (error instanceof ContractDriftError) {
    return 'contract-drift';
  }

  if (error?.name === 'HttpStatusError') {
    const status = Number(error.status);
    if (status >= 500 || [408, 425, 429].includes(status)) {
      return 'service-outage';
    }
    return 'contract-drift';
  }

  if (
    ['AbortError', 'TimeoutError', 'TypeError'].includes(error?.name) ||
    ['ECONNRESET', 'ECONNREFUSED', 'ENOTFOUND', 'EAI_AGAIN', 'ETIMEDOUT'].includes(error?.code)
  ) {
    return 'service-outage';
  }

  return 'verification-error';
}

export function exitCodeForConclusion(conclusion, failureClasses = []) {
  if (conclusion === 'passed') return 0;
  if (conclusion === 'service-outage' && failureClasses.length === 1) return 2;
  return 1;
}
