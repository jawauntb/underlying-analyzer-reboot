import { createHash } from 'node:crypto';
import { lstat, readFile, readdir } from 'node:fs/promises';
import { basename, extname, relative, resolve } from 'node:path';

const IGNORED_DIRECTORIES = new Set(['.git', '.expo', '.artifacts', 'node_modules']);
const BINARY_EXTENSIONS = new Set([
  '.gif',
  '.heic',
  '.ico',
  '.jpeg',
  '.jpg',
  '.otf',
  '.pdf',
  '.png',
  '.ttf',
  '.webp',
  '.woff',
  '.woff2',
]);
const FORBIDDEN_EXTENSIONS = new Set([
  '.jks',
  '.key',
  '.keystore',
  '.mobileprovision',
  '.p12',
  '.p8',
  '.pem',
]);

const SECRET_PATTERNS = [
  { id: 'openai-or-anthropic-key', pattern: /\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}\b/g },
  { id: 'github-token', pattern: /\bgh[pousr]_[A-Za-z0-9]{30,}\b/g },
  { id: 'aws-access-key', pattern: /\bAKIA[0-9A-Z]{16}\b/g },
  { id: 'google-api-key', pattern: /\bAIza[0-9A-Za-z_-]{35}\b/g },
  { id: 'stripe-live-secret', pattern: /\bsk_live_[A-Za-z0-9]{20,}\b/g },
  { id: 'slack-token', pattern: /\bxox[baprs]-[A-Za-z0-9-]{20,}\b/g },
  { id: 'private-key-block', pattern: /-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----/g },
];

export const DEFAULT_SCAN_LIMITS = Object.freeze({
  maxFiles: 20_000,
  maxFileBytes: 25 * 1024 * 1024,
  maxTotalBytes: 300 * 1024 * 1024,
});

export function secretPatternIds(text) {
  return SECRET_PATTERNS.flatMap(({ id, pattern }) => {
    pattern.lastIndex = 0;
    return pattern.test(text) ? [id] : [];
  });
}

function isForbiddenPath(path) {
  const name = basename(path).toLowerCase();
  return (
    FORBIDDEN_EXTENSIONS.has(extname(name)) ||
    name === '.env' ||
    (name.startsWith('.env.') && name !== '.env.example')
  );
}

async function collectFiles(root, current, files, findings, limits) {
  const stat = await lstat(current);
  const displayPath = relative(root, current) || '.';
  if (stat.isSymbolicLink()) {
    findings.push({ path: displayPath, rule: 'artifact.symlink' });
    return;
  }
  if (stat.isFile()) {
    files.push(current);
    if (files.length > limits.maxFiles) {
      throw new Error(`Artifact scan exceeded ${limits.maxFiles} files.`);
    }
    return;
  }
  if (!stat.isDirectory()) return;

  const entries = await readdir(current, { withFileTypes: true });
  for (const entry of entries) {
    if (entry.isDirectory() && IGNORED_DIRECTORIES.has(entry.name)) continue;
    await collectFiles(root, resolve(current, entry.name), files, findings, limits);
  }
}

export async function scanRoots(rootPaths, limits = DEFAULT_SCAN_LIMITS) {
  const findings = [];
  const files = [];
  const roots = rootPaths.map((root) => resolve(root));

  for (const root of roots) {
    await collectFiles(root, root, files, findings, limits);
  }

  let totalBytes = 0;
  let textFiles = 0;
  let binaryFiles = 0;
  const aggregate = createHash('sha256');

  for (const file of files.sort()) {
    const stat = await lstat(file);
    const root = roots.find((candidate) => file === candidate || file.startsWith(`${candidate}/`)) ?? roots[0];
    const displayPath = `${basename(root)}/${relative(root, file) || basename(file)}`;
    totalBytes += stat.size;
    if (totalBytes > limits.maxTotalBytes) {
      throw new Error(`Artifact scan exceeded ${limits.maxTotalBytes} total bytes.`);
    }
    if (stat.size > limits.maxFileBytes) {
      findings.push({ path: displayPath, rule: 'artifact.oversized' });
      continue;
    }
    if (isForbiddenPath(file)) {
      findings.push({ path: displayPath, rule: 'artifact.private-file' });
      continue;
    }

    const contents = await readFile(file);
    aggregate.update(displayPath);
    aggregate.update('\0');
    aggregate.update(createHash('sha256').update(contents).digest());
    if (BINARY_EXTENSIONS.has(extname(file).toLowerCase()) || contents.includes(0)) {
      binaryFiles += 1;
      continue;
    }

    textFiles += 1;
    const text = contents.toString('utf8');
    for (const id of secretPatternIds(text)) {
      findings.push({ path: displayPath, rule: `secret.${id}` });
    }
  }

  return {
    roots: roots.map((root) => basename(root)),
    files: files.length,
    textFiles,
    binaryFiles,
    totalBytes,
    aggregateSha256: aggregate.digest('hex'),
    findings,
  };
}
