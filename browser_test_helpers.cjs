const fs = require('node:fs');
const {spawnSync} = require('node:child_process');

function chromePath() {
  const candidates = [process.env.CHROME_PATH,
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Chromium.app/Contents/MacOS/Chromium'];
  for (const name of ['google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser']) {
    const found = spawnSync('which', [name], {encoding: 'utf8'});
    if (found.status === 0) candidates.push(found.stdout.trim());
  }
  const found = candidates.find(candidate => candidate && fs.existsSync(candidate));
  if (!found) throw new Error('Chrome or Chromium not found; set CHROME_PATH to its executable.');
  return found;
}

async function closeBrowser(browser) {
  const chromeProcess = browser.process();
  try {
    await browser.close();
  } finally {
    // Chrome helpers can inherit these pipes after the browser exits.
    for (const stream of [chromeProcess?.stdin, chromeProcess?.stdout, chromeProcess?.stderr]) stream?.destroy();
  }
}

module.exports = {puppeteer: require('puppeteer-core'), chromePath, closeBrowser};
