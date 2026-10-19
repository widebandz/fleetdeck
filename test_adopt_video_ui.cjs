// Exercise the actual page script with a minimal DOM and controlled API failures.
// This does not replace visual/browser verification.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.attributes = {}; this._text = ''; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent ?? String(c)).join(''); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this._text = ''; this.children = nodes; }
  setAttribute(key, value) { this.attributes[key] = value; }
  focus() {}
  scrollIntoView() {}
}

function page(responses, hash = '') {
  const elements = new Map(['#detail', '#inspect', '#error', '#checks', '#capacity', '#link-support', '#source-limits', '#jobs'].map(id => [id, new Element('div')]));
  const document = {
    createElement: tag => new Element(tag),
    createTextNode: text => ({textContent: text}),
    querySelector: selector => elements.get(selector),
  };
  const context = vm.createContext({document, console, navigator: {}, location: {hash},
    fetch: async url => {
      const value = responses[url];
      if (value instanceof Error) throw value;
      if (value === undefined) throw Error('Unexpected API request: ' + url);
      return {ok: true, json: async () => value};
    },
  });
  const html = fs.readFileSync(path.join(__dirname, 'adopt_video.html'), 'utf8');
  const script = html.match(/<script>([\s\S]*?)<\/script>/)[1]
    .replace('refresh();setInterval(refresh,2000);', '');
  vm.runInContext(script, context);
  return {elements, context, render: async job => {
    context.fixtureJob = job;
    await vm.runInContext('selected=fixtureJob.id; capabilities={organize:true,skill_repo:true}; render(fixtureJob)', context);
    return elements.get('#detail');
  }};
}

function job(overrides = {}) {
  return {id: 'VID-0123456789abcdef', title: 'Saved reel', updated: 1,
    state: 'transcript_ready', stage: 'plan', message: 'Transcript ready.',
    stages: ['download', 'audio', 'transcribe', 'plan', 'package'],
    transcript: {status: 'ready'}, files: {segments: '/segments'}, ...overrides};
}

const segments = [{id: 'S0001', start_ms: 0, end_ms: 1000, text: 'Actual transcript evidence.'}];

test('expanded-link readiness reflects the running server capability', () => {
  const ui = page({});
  ui.context.preflight = {tools: {}, whisper_model_present: true, worker_running: true, free_bytes: 10 * 1024**3};
  vm.runInContext('readiness(preflight)', ui.context);
  assert.equal(ui.elements.get('#link-support').hidden, false);
  assert.match(ui.elements.get('#source-limits').textContent, /Up to 20 minutes/);
  ui.context.preflight.capabilities = {public_video_pages: true, long_video: true, repository_references: true};
  ui.context.preflight.limits = {minutes: 90, video_mib: 500};
  vm.runInContext('readiness(preflight)', ui.context);
  assert.equal(ui.elements.get('#link-support').hidden, true);
  assert.match(ui.elements.get('#source-limits').textContent, /Up to 90 minutes/);
});

test('a private job link opens its transcript rather than the latest job', async () => {
  const target = job({title: 'Requested pilot'});
  const newer = job({id: 'VID-aaaaaaaaaaaaaaaa', title: 'Newer unrelated video', files: {}});
  const ui = page({'/segments': segments, '/api/video/state': {token: 'fixture', jobs: [newer, target],
    preflight: {tools: {}, worker_running: true, free_bytes: 10 * 1024**3,
      capabilities: {public_video_pages: true, organize: true}}}}, '#' + target.id);
  await vm.runInContext('refresh()', ui.context);
  assert.match(ui.elements.get('#detail').textContent, /Requested pilot/);
  assert.match(ui.elements.get('#detail').textContent, /Actual transcript evidence\./);
  assert.doesNotMatch(ui.elements.get('#detail').textContent, /Newer unrelated video/);
});

function descendants(element) {
  return [element, ...element.children.flatMap(child => child instanceof Element ? descendants(child) : [])];
}

test('a failed secondary artifact does not hide the transcript', async () => {
  const ui = page({'/segments': segments, '/plan': new Error('Plan unavailable')});
  const root = await ui.render(job({state: 'plan_review', files: {segments: '/segments', plan: '/plan'}}));
  assert.match(root.textContent, /Actual transcript evidence\./);
  assert.match(root.textContent, /Plan unavailable/);
});

test('transcript displays before a slow secondary artifact finishes loading', async () => {
  let finishPlan;
  const pending = new Promise(resolve => { finishPlan = resolve; });
  const ui = page({'/segments': segments, '/plan': pending});
  const rendering = ui.render(job({state: 'plan_review', files: {segments: '/segments', plan: '/plan'}}));
  await new Promise(resolve => setImmediate(resolve));
  assert.match(ui.elements.get('#detail').textContent, /Actual transcript evidence\./);
  finishPlan({title: 'Draft plan', goal: 'Review evidence', inputs: [], steps: [],
    success_criteria: [], open_questions: [], structure_principle: 'ICM'});
  await rendering;
});

test('partial transcript remains readable with organize and repo actions gated', async () => {
  const ui = page({'/segments': segments});
  const root = await ui.render(job({state: 'needs_attention', stage: 'transcribe',
    transcript: {status: 'partial', processed_seconds: 30, total_seconds: 60}}));
  assert.match(root.textContent, /Actual transcript evidence\./);
  assert.match(root.textContent, /0:30 of 1:00 processed/);
  assert.match(root.textContent, /Processing stopped; completed text is saved/);
  const buttons = descendants(root).filter(e => e.tagName === 'button').map(e => e.textContent);
  assert(!buttons.includes('Organize transcript'));
  assert(!buttons.includes('Build skill repository'));
});

test('a completed transcript offers organize while preserving literal source text', async () => {
  const ui = page({'/segments': [{...segments[0], text: '<script>untrusted()</script>'}]});
  const root = await ui.render(job());
  assert.match(root.textContent, /<script>untrusted\(\)<\/script>/);
  assert(descendants(root).some(e => e.tagName === 'button' && e.textContent === 'Organize transcript'));
  assert(!descendants(root).some(e => e.tagName === 'script'));
});

test('repo references show quotes and timestamps without activating unsafe URLs', async () => {
  const ui = page({'/segments': segments, '/references': {note: 'Unverified source claims.',
    repositories: [{url: 'https://github.com/acme/tool', evidence: [{kind: 'transcript',
      segment_id: 'S0001', start_ms: 3540000, quote: '<script>untrusted()</script>'}]},
      {url: 'javascript:untrusted()', evidence: []}],
    unresolved_mentions: [{start_ms: 3550000, quote: 'Another repo is named in speech.'}]}});
  const root = await ui.render(job({files: {segments: '/segments', references: '/references'}}));
  assert.match(root.textContent, /Repositories mentioned/);
  assert.match(root.textContent, /https:\/\/github.com\/acme\/tool/);
  assert.match(root.textContent, /S0001 · 59:00/);
  assert.match(root.textContent, /Another repo is named in speech/);
  assert(!descendants(root).some(e => e.href?.startsWith('javascript:')));
  assert(!descendants(root).some(e => e.tagName === 'script'));
});

test('a failed repo-reference artifact leaves the transcript visible', async () => {
  const ui = page({'/segments': segments, '/references': new Error('References unavailable')});
  const root = await ui.render(job({files: {segments: '/segments', references: '/references'}}));
  assert.match(root.textContent, /Actual transcript evidence\./);
  assert.match(root.textContent, /References unavailable/);
});
