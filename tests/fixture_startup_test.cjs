const {test} = require('node:test');
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {fixtureURL} = require('./fixture_startup.cjs');

function child(program) {
  return spawn(process.execPath, ['-e', program], {stdio: ['ignore', 'pipe', 'pipe']});
}

test('fixture readiness survives split stdout chunks', async () => {
  const service = child("process.stdout.write('http://127.0.0.1:12'); setTimeout(() => console.log('345'), 30)");
  assert.equal(await fixtureURL(service), 'http://127.0.0.1:12345');
});

test('fixture exit reports code and stderr', async () => {
  const service = child("console.error('Synthetic configuration failure'); process.exitCode = 7");
  await assert.rejects(fixtureURL(service), /Fixture exited 7[\s\S]*Synthetic configuration failure/);
});

test('startup timeout includes diagnostics and does not hang the runner', async () => {
  const service = child("console.error('Synthetic startup stalled'); setInterval(() => {}, 1000)");
  try {
    await assert.rejects(fixtureURL(service, 1000), /startup timed out[\s\S]*Synthetic startup stalled/);
  } finally {
    service.kill();
  }
});

test('missing executable is reported immediately', async () => {
  const service = spawn('/nonexistent/murmuratio-test-python', [], {stdio: ['ignore', 'pipe', 'pipe']});
  await assert.rejects(fixtureURL(service), /Fixture could not start:[\s\S]*ENOENT/);
});
