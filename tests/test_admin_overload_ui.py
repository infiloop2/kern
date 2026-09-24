"""Exercise the actual browser API module's cooldown state machine in Node."""
import json
from pathlib import Path
import subprocess
import shutil
import playwright
import unittest


class OverloadUiTests(unittest.TestCase):
    def test_cooldown_backoff_races_and_no_write_replay(self):
        source = (Path(__file__).parents[1] / 'host/runtime/admin_api/admin_ui/api.js').read_text()
        source = source.replace('const READ_DEADLINE_MS = 30000;', 'const READ_DEADLINE_MS = 30;')
        script = '''
import assert from 'node:assert/strict';
globalThis.window = {addEventListener() {}};
let now = 100000;
Date.now = () => now;
const source = SOURCE;
const api = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
const notices = [];
api.setAvailabilityHandler(message => notices.push(message));
let calls = 0;
let finishOld;
globalThis.fetch = async () => {
  calls++;
  if (calls === 1) return new Promise(resolve => {finishOld = resolve;});
  return new Response('{"error":{"code":"host_busy"}}',
    {status:503, headers:{'Retry-After':'5', 'Content-Type':'application/json'}});
};
const old = api.api('GET', '/old');
await assert.rejects(api.api('GET', '/busy'), e => e.code === 'host_unavailable' && e.status === 503);
finishOld(new Response('{}'));
await old;
assert.equal(api.isOverloadCoolingDown(), true, 'old success cannot clear cooldown');
await assert.rejects(api.api('POST', '/write', {value:1}), e => e.code === 'host_unavailable');
assert.equal(calls, 3, 'operator action gets one attempt during read cooldown');
now += 5000;
await assert.rejects(api.api('GET', '/busy'), e => e.status === 503);
now += 9999;
assert.equal(api.isOverloadCoolingDown(), true, 'second failure backs off for ten seconds');
now += 1;
assert.equal(api.isOverloadCoolingDown(), false);
globalThis.fetch = async () => { calls++; return new Response('{}'); };
await api.api('GET', '/recovered');
assert.equal(notices.at(-1), '');
assert.equal(calls, 5, 'failed write was never replayed');
let unauthorized = 0;
api.setUnauthorizedHandler(() => unauthorized++);
globalThis.fetch = async () => new Response('{}', {status:401});
await assert.rejects(api.api('GET', '/auth'), /unauthorized/);
assert.equal(unauthorized, 1);
assert.equal(api.isOverloadCoolingDown(), false);
globalThis.fetch = async () => { throw new TypeError('network failed'); };
await assert.rejects(api.api('GET', '/network'), e => e.code === 'host_unavailable');
assert.equal(api.isOverloadCoolingDown(), false, 'one network failure must not pause reads');
globalThis.fetch = async () => new Response('bad gateway', {status:502});
await assert.rejects(api.apiBlob('/file'), e => e.status === 502);
assert.equal(api.isOverloadCoolingDown(), false);
globalThis.fetch = async () => new Response(JSON.stringify({error:{message:'Usage temporarily unavailable'}}),
  {status:503, headers:{'Content-Type':'application/json'}});
await assert.rejects(api.api('GET', '/analytics'), e => e.status === 503 && e.message === 'Usage temporarily unavailable');
assert.equal(api.isOverloadCoolingDown(), false, 'one endpoint failure must not pause other sections');
globalThis.fetch = async () => new Response(JSON.stringify({error:{message:'Busy', code:'host_busy'}}),
  {status:503, headers:{'Content-Type':'application/json'}});
await assert.rejects(api.api('GET', '/full'), e => e.code === 'host_unavailable');
assert.equal(api.isOverloadCoolingDown(), true);
let logoutCalls = 0;
globalThis.fetch = async path => {
  assert.equal(path, '/v1/logout');
  logoutCalls++;
  return new Response('busy', {status:503});
};
await assert.rejects(api.logout(), e => e.code === 'host_unavailable');
assert.equal(logoutCalls, 1, 'logout must reach the host even during cooldown');
now += 5000;
globalThis.fetch = async (path, options) => {
  assert.equal(path, '/stalled');
  return new Promise((resolve, reject) => {
    options.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
  });
};
await assert.rejects(api.api('GET', '/stalled'), e => e.code === 'host_unavailable');
assert.equal(api.isOverloadCoolingDown(), false, 'one stalled read must not pause reads');
globalThis.fetch = async () => new Response('<html>Busy</html>', {status:503});
await assert.rejects(api.workspaceHtml('/workspace/chat.html'), e => e.code === 'host_unavailable');
now += 10000;
globalThis.fetch = async () => new Response('<div>Chat</div>');
assert.equal(await api.workspaceHtml('/workspace/chat.html'), '<div>Chat</div>');
process.exit(0);
'''.replace('SOURCE', json.dumps(source), 1)
        # CI already ships Playwright's Node driver, but no node on PATH.
        node = shutil.which('node') or str(Path(playwright.__file__).parent / 'driver' / 'node')
        result = subprocess.run([node, '--input-type=module'], input=script, text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
