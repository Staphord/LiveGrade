const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');

test('public leaderboard refresh client requests only its revealed JSON projection', () => {
  const source = fs.readFileSync(require.resolve('../live_leaderboard.js'), 'utf8');
  assert.match(source, /Accept:\s*'application\/json'/);
  assert.match(source, /credentials:\s*'omit'/);
  assert.match(source, /setInterval\(refresh,\s*30000\)/);
  assert.match(source, /textContent\s*=/);
  assert.doesNotMatch(source, /innerHTML/);
});
