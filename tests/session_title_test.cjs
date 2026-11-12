'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const context = vm.createContext({window: {}});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/now.js'), 'utf8'), context);

assert.equal(context.sessionTitleDisplay('↳ film: X'), '→ film: X');
assert.equal(context.sessionTitleDisplay('film: X'), 'film: X');
assert.equal(context.sessionTitleDisplay('↳film: X'), '→ film: X');
assert.equal(context.sessionTitleDisplay('↳   film: X'), '→ film: X');
assert.equal(context.sessionTitleDisplay('film: ↳ X'), 'film: ↳ X');
assert.equal(context.sessionTitleDisplay('↳   Keep title exactly', 'launcher'), '↳   Keep title exactly');
assert.equal(context.sessionTitleDisplay('Mode:   Keep title exactly', 'launcher'), 'Mode:   Keep title exactly');
console.log('Session title display: 7 cases passed');

assert.equal(context.modelLabel({kind: 'codex', model: 'gpt-6.1-sol', effort: 'high'}), 'Sol high');
assert.equal(context.modelLabel({kind: 'claude', model: 'claude-opus-5-5', effort: 'xhigh'}), 'Opus 5.5 xhigh');
assert.equal(context.modelLabel({kind: 'claude', model: 'claude-sonnet-5-5', effort: null}), 'Sonnet 5.5');
assert.equal(context.modelLabel({kind: 'claude', model: 'claude-sonnet-5-5', effort: null}, true), 'Sonnet 5.5 effort unknown');
assert.equal(context.modelLabel({kind: 'codex', model: null, who: 'Codex sol'}), 'model unknown');
assert.equal(context.modelLabel({kind: 'claude', model: null, who: 'Claude Opus 5.5'}, true), 'model unknown effort unknown');
console.log('Recorded model and effort display: 6 cases passed');
