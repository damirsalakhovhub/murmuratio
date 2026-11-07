const assert = require('node:assert/strict');
const fs = require('node:fs');
const net = require('node:net');
const path = require('node:path');
const {spawn, spawnSync} = require('node:child_process');

const root = path.resolve(__dirname, '..');
const cli = path.join(root, 'murmuratio');

function isolatedEnv(home) {
  const bin = path.join(home, 'bin');
  fs.mkdirSync(bin, {recursive: true});
  // HOME does not isolate the real launchd namespace. All CLI tests shadow launchctl.
  fs.writeFileSync(path.join(bin, 'launchctl'), `#!/bin/sh
printf '%s\\n' "$1" >> "$MURMURATIO_LAUNCHCTL_LOG"
case "$1" in
  print) [ -f "$MURMURATIO_LAUNCHCTL_STATE" ] && [ "$(cat "$MURMURATIO_LAUNCHCTL_STATE")" = loaded ] ;;
  bootstrap) printf loaded > "$MURMURATIO_LAUNCHCTL_STATE" ;;
  bootout) printf stopped > "$MURMURATIO_LAUNCHCTL_STATE" ;;
  *) exit 1 ;;
esac
`, {mode: 0o755});
  const env = {...process.env, HOME: home, CODEX_HOME: path.join(home, '.codex'),
    PATH: `${bin}${path.delimiter}${process.env.PATH}`, PYTHONDONTWRITEBYTECODE: '1',
    MURMURATIO_LAUNCHCTL_LOG: path.join(home, 'launchctl.log'),
    MURMURATIO_LAUNCHCTL_STATE: path.join(home, 'launchctl.state')};
  delete env.PORTLESS_URL;
  delete env.PYTHONPATH;
  return env;
}

function runCli(env, ...args) {
  const result = spawnSync(cli, args, {cwd: root, env, encoding: 'utf8', timeout: 15000});
  if (result.error) throw result.error;
  return result;
}

function success(result) {
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
  return result;
}

async function reservePort() {
  const socket = net.createServer();
  await new Promise((resolve, reject) => {
    socket.once('error', reject);
    socket.listen(0, '127.0.0.1', resolve);
  });
  return socket;
}

async function startServer(env) {
  const socket = await reservePort();
  const port = socket.address().port;
  await new Promise(resolve => socket.close(resolve));
  const server = spawn(cli, ['run', '--port', String(port)], {cwd: root, env, stdio: ['ignore', 'pipe', 'pipe']});
  let stderr = '';
  server.stderr.on('data', chunk => { stderr += chunk; });
  try {
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(`Server did not start: ${stderr}`)), 15000);
      server.once('error', error => { clearTimeout(timer); reject(error); });
      server.once('exit', code => { clearTimeout(timer); reject(new Error(`Server exited (${code}): ${stderr}`)); });
      server.stdout.on('data', chunk => {
        if (String(chunk).includes(`http://127.0.0.1:${port}`)) {
          clearTimeout(timer);
          resolve();
        }
      });
    });
  } catch (error) {
    server.kill();
    throw error;
  }
  return {server, url: `http://127.0.0.1:${port}`};
}

async function stopServer(server) {
  if (!server || server.exitCode !== null || server.signalCode !== null) return;
  const closed = new Promise(resolve => server.once('close', resolve));
  server.kill();
  await closed;
}

module.exports = {isolatedEnv, reservePort, root, runCli, startServer, stopServer, success};
