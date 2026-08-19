import assert from 'node:assert/strict';
import { mkdir, mkdtemp, rm, symlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';

import { scanRoots, secretPatternIds } from './artifact-policy.mjs';

test('secret classifier detects credential forms without returning secret material', () => {
  const sample = `token=${'sk-' + 'A'.repeat(30)}`;
  assert.deepEqual(secretPatternIds(sample), ['openai-or-anthropic-key']);
});

test('secret classifier detects private key blocks', () => {
  const sample = `${'-----BEGIN PRIVATE ' + 'KEY-----'}\nnot-a-real-key`;
  assert.deepEqual(secretPatternIds(sample), ['private-key-block']);
});

test('secret classifier permits documentation placeholders', () => {
  assert.deepEqual(secretPatternIds('OPENAI_API_KEY=sk-proj-... your-key-here'), []);
});

test('artifact traversal inventories clean text and binary files deterministically', async (t) => {
  const root = await mkdtemp(join(tmpdir(), 'undercurrent-artifacts-clean-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  await writeFile(join(root, 'readme.txt'), 'safe artifact\n');
  await writeFile(join(root, 'pixel.bin'), Buffer.from([0, 1, 2, 3]));

  const first = await scanRoots([root]);
  const second = await scanRoots([root]);

  assert.deepEqual(first.findings, []);
  assert.equal(first.files, 2);
  assert.equal(first.textFiles, 1);
  assert.equal(first.binaryFiles, 1);
  assert.equal(first.aggregateSha256, second.aggregateSha256);
});

test('artifact traversal reports secrets, private files, symlinks, and size bounds', async (t) => {
  const root = await mkdtemp(join(tmpdir(), 'undercurrent-artifacts-policy-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  await writeFile(join(root, 'secret.txt'), `token=${'sk-' + 'A'.repeat(30)}`);
  await writeFile(join(root, '.env'), 'SAFE_PLACEHOLDER=true\n');
  await writeFile(join(root, 'signing.p8'), 'placeholder\n');
  await writeFile(join(root, 'oversized.txt'), 'x'.repeat(65));
  await symlink(join(root, 'secret.txt'), join(root, 'secret-link'));
  await mkdir(join(root, '.git'));
  await writeFile(join(root, '.git', 'ignored.txt'), `token=${'sk-' + 'B'.repeat(30)}`);

  const result = await scanRoots([root], {
    maxFiles: 20,
    maxFileBytes: 64,
    maxTotalBytes: 1024,
  });
  const rules = result.findings.map(({ rule }) => rule).sort();

  assert.deepEqual(rules, [
    'artifact.oversized',
    'artifact.private-file',
    'artifact.private-file',
    'artifact.symlink',
    'secret.openai-or-anthropic-key',
  ]);
  assert.equal(result.files, 4);
});

test('artifact traversal enforces file and aggregate byte ceilings', async (t) => {
  const root = await mkdtemp(join(tmpdir(), 'undercurrent-artifacts-limits-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  await writeFile(join(root, 'a.txt'), '1234');
  await writeFile(join(root, 'b.txt'), '5678');

  await assert.rejects(
    scanRoots([root], { maxFiles: 1, maxFileBytes: 100, maxTotalBytes: 100 }),
    /exceeded 1 files/,
  );
  await assert.rejects(
    scanRoots([root], { maxFiles: 10, maxFileBytes: 100, maxTotalBytes: 7 }),
    /exceeded 7 total bytes/,
  );
});
