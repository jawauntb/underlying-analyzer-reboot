import { mkdir, writeFile } from 'node:fs/promises';
import { dirname } from 'node:path';

import { scanRoots } from './artifact-policy.mjs';

function parseArguments(argv) {
  const roots = [];
  let receiptPath = '.artifacts/receipts/artifact-scan.json';
  for (let index = 0; index < argv.length; index += 1) {
    if (argv[index] === '--receipt') {
      receiptPath = argv[index + 1];
      index += 1;
    } else {
      roots.push(argv[index]);
    }
  }
  if (roots.length === 0) {
    throw new Error('Provide at least one source or artifact root to scan.');
  }
  return { receiptPath, roots };
}

const { receiptPath, roots } = parseArguments(process.argv.slice(2));
const startedAt = new Date().toISOString();
let conclusion = 'passed';
let scan;

try {
  scan = await scanRoots(roots);
  if (scan.findings.length > 0) conclusion = 'policy-failed';
} catch (error) {
  conclusion = 'scanner-error';
  scan = {
    roots,
    files: 0,
    textFiles: 0,
    binaryFiles: 0,
    totalBytes: 0,
    aggregateSha256: null,
    findings: [{ path: '.', rule: `scanner.${error?.name ?? 'unknown'}` }],
  };
}

const receipt = {
  schemaVersion: 1,
  kind: 'undercurrent-secret-artifact-scan',
  conclusion,
  startedAt,
  completedAt: new Date().toISOString(),
  runtime: {
    node: process.version,
    ci: process.env.CI === 'true',
    gitSha: process.env.GITHUB_SHA ?? null,
    runId: process.env.GITHUB_RUN_ID ?? null,
  },
  scan,
};

await mkdir(dirname(receiptPath), { recursive: true });
await writeFile(receiptPath, `${JSON.stringify(receipt, null, 2)}\n`, { mode: 0o600 });
console.log(
  `${conclusion.toUpperCase()} scanned ${scan.files} files (${scan.totalBytes} bytes); metadata receipt: ${receiptPath}`,
);

if (conclusion !== 'passed') {
  for (const finding of scan.findings) {
    console.error(`::error file=${finding.path},title=Secret/artifact policy::${finding.rule}`);
  }
  process.exitCode = 1;
}
