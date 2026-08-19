import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const readJson = async (relativePath) =>
  JSON.parse(await readFile(new URL(relativePath, import.meta.url), 'utf8'));

test('production release configuration remains eligible for TestFlight', async () => {
  const [app, eas] = await Promise.all([
    readJson('../../app.json'),
    readJson('../../eas.json'),
  ]);

  assert.equal(app.expo.ios.bundleIdentifier, 'com.theunderlying.undercurrent');
  assert.equal(app.expo.ios.config.usesNonExemptEncryption, false);
  assert.equal(eas.cli.appVersionSource, 'remote');
  assert.deepEqual(eas.build.production, {
    node: '22.16.0',
    environment: 'production',
    autoIncrement: true,
    distribution: 'store',
  });
  assert.deepEqual(eas.submit.production, {});
});
