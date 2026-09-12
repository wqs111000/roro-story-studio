const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

test('summary-only library loads detail and ignores an older response', async () => {
  const html = fs.readFileSync(path.join(__dirname, '../service/ui/workbench.html'), 'utf8');
  const start = html.indexOf('async function openLibraryDetail(id)');
  const end = html.indexOf('async function loadLibrary()', start);
  assert.ok(start >= 0 && end > start);
  const nodes = {}, pending = [];
  const context = vm.createContext({
    state: {},
    $: id => nodes[id] ||= {},
    openModal: () => {},
    escapeHtml: text => String(text),
    renderSpokenLines: page => page.spoken_text,
    readStudioJSON: url => new Promise(resolve => pending.push({url, resolve})),
  });
  vm.runInContext(html.slice(start, end), context);
  const first = context.openLibraryDetail('draft-one');
  const second = context.openLibraryDetail('draft-two');
  assert.equal(pending[1].url, '/api/studio/story-library/draft-two');
  pending[1].resolve({title:'Second', pages:[{page:1, spoken_text:'Dialogue and interaction'}]});
  await second;
  assert.match(nodes['story-library-modal-pages'].innerHTML, /Dialogue and interaction/);
  pending[0].resolve({title:'First', pages:[]});
  await first;
  assert.equal(nodes['story-library-modal-title'].textContent, 'Second');
});
