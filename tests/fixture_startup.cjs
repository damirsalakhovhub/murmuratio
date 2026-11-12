// Capture startup diagnostics from the moment a fixture process is spawned.
function fixtureURL(service, timeout = 30000) {
  return new Promise((resolve, reject) => {
    let stdout = '', stderr = '';
    const started = Date.now();
    const describe = reason => new Error(`${reason} after ${Date.now() - started}ms\n` +
      `Command: ${service.spawnargs.join(' ')}\nstdout:\n${stdout || '(empty)'}\nstderr:\n${stderr || '(empty)'}`);
    const cleanup = () => {
      clearTimeout(timer);
      service.removeListener('error', onError);
      service.removeListener('close', onClose);
      service.stdout.removeListener('data', onOutput);
      service.stderr.removeListener('data', onStderr);
      // Keep draining pipes during the browser run so a fixture cannot block on logging.
      service.stdout.resume();
      service.stderr.resume();
    };
    const fail = reason => { const error = describe(reason); cleanup(); reject(error); };
    const onError = error => fail(`Fixture could not start: ${error.message}`);
    const onClose = (code, signal) => fail(`Fixture exited ${signal || code}`);
    const onStderr = chunk => { stderr = (stderr + chunk).slice(-16384); };
    const onOutput = chunk => {
      stdout = (stdout + chunk).slice(-16384);
      const match = stdout.match(/http:\/\/127\.0\.0\.1:\d+(?=\s)/);
      if (match) { cleanup(); resolve(match[0]); }
    };
    const timer = setTimeout(() => fail('Fixture startup timed out'), timeout);
    service.on('error', onError);
    service.on('close', onClose);
    service.stdout.on('data', onOutput);
    service.stderr.on('data', onStderr);
  });
}

module.exports = {fixtureURL};
