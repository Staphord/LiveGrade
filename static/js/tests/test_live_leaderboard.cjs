const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');

test('public leaderboard uses silent websocket updates and a safe JSON projection', () => {
  const source = fs.readFileSync(require.resolve('../live_leaderboard.js'), 'utf8');
  assert.match(source, /Accept:\s*'application\/json'/);
  assert.match(source, /credentials:\s*'omit'/);
  assert.match(source, /new window\.WebSocket/);
  assert.match(source, /leaderboard\.state/);
  assert.match(source, /scheduleReconnect/);
  assert.doesNotMatch(source, /setInterval/);
  assert.match(source, /textContent\s*=/);
  assert.doesNotMatch(source, /innerHTML/);
});
