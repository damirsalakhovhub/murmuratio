'use strict';
(() => {
  const node = (tag, className = '', value) => {
    const el = document.createElement(tag);
    el.className = className;
    if (value !== undefined) el.textContent = value;
    return el;
  };
  const text = (el, value) => { if (el.textContent !== value) el.textContent = value; };
  const nowView = window.createNowView({node, text});
  let configKey = '', busy = false;

  async function api(path) {
    const response = await fetch(path, {cache: 'no-store', credentials: 'same-origin'});
    if (!response.ok) throw new Error('Source unavailable');
    return response.json();
  }
  async function poll() {
    if (busy) return;
    busy = true;
    try {
      const config = await api('/api/config');
      if (JSON.stringify(config) !== configKey) { location.reload(); return; }
      nowView.render(await api('/api/now'));
    } catch { nowView.error(); }
    finally { busy = false; }
  }
  async function start() {
    try {
      const config = await api('/api/config');
      configKey = JSON.stringify(config); nowView.configure(config);
    } catch { nowView.error(); setTimeout(start, 3000); return; }
    void poll();
    setInterval(() => { if (!document.hidden) void poll(); }, BOARD_REFRESH_MS);
  }
  void start();
})();
