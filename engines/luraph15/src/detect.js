'use strict';

const LURAPH_HEADER = /This file was protected using Luraph Obfuscator v(\d+)(?:\.(\d+))?/;
const LURAPH_VM_SHAPE = /\[\d+\]=(bit32|buffer|string|table|math)\.\w+/;
// v15 chunks primed by loader stubs (Luarmor etc.) start with loader lines
// (script_key/_bsdata0/blob tables); the VM chunk starts later with
// `return setmetatable({` — usually at a line start, sometimes directly
// after the stub's closing long-bracket (`]]]]return setmetatable...`).
// Scan the first 64 KB for that boundary (the VM-shape check below is the
// real validator).
const VM_CHUNK_START = /[\s\]]*return setmetatable\(\{/;

// v15 VM present after the header? (non-virtualized LPH_NO_VIRTUALIZE builds
// start with `return({function(...)` and plain code instead). Without this a
// header-only file scored 1.0, was routed to the lifter and came back empty
// after burning the whole job budget.
function hasVm(source, from = 0) {
  const head = source.slice(from, from + 65536);
  return VM_CHUNK_START.test(head) || LURAPH_VM_SHAPE.test(head.slice(0, 4000));
}

function detectLuraph(source) {
  // The header is normally line 1, but re-uploads often prepend banner comments
  // ("-- skid skid ...") or loader lines (pcall(loadstring(HttpGet ...))) above it,
  // so look for it anywhere in the first 64 KB.
  const head = source.slice(0, 65536);
  const m = LURAPH_HEADER.exec(head);
  if (m) {
    return m[1] === '15' ? (hasVm(source, m.index) ? 1.0 : 0.45) : 0.3;
  }
  // headerless `return({...})` object-table VM (helper-method dispatch)
  const body = head.replace(/^(?:\s*--[^\n]*\n)+/, '').replace(/^\s+/, '');
  if (/^return\s*\(\s*\{/.test(body) && /bit32|buffer\.|loadstring/.test(source.slice(0, 200000))
      && /\}\s*\)\s*:\s*[A-Za-z_]\w*\s*\(\s*\.\.\.\s*\)\s*$/.test(source.slice(-400))) {
    return 0.7;
  }
  const vm = VM_CHUNK_START.exec(head);
  if (vm) {
    const at = vm.index + vm[0].length - 'return setmetatable({'.length;
    const chunkHead = source.slice(at, at + 2000);
    if (LURAPH_VM_SHAPE.test(chunkHead) || source.slice(at, at + 200000).includes('LPH')) {
      return 0.8;
    }
  }
  return 0.0;
}

const HEADER_LINE_RE = /\s*--[ \t]*This file was protected using Luraph Obfuscator v[\d.]+[ \t]*\[https?:\/\/lura\.ph\/?\]/;

function restoreHeaderNewline(source) {
  const m = HEADER_LINE_RE.exec(source);
  if (m) {
    const end = m.index + m[0].length;
    const next = source[end];
    if (next !== '' && next !== '\n' && next !== '\r') {
      return source.slice(0, end) + '\n' + source.slice(end).replace(/^[ \t]+/, '');
    }
  }
  return source;
}

// Luarmor whitelist client (see docs/LUARMOR_NOTES.md §9 for the rationale).
// These files are auth-gated loaders around a Luraph-VM constant layer; they
// are not devirtualizable, so detection exists to classify and route them.
const LUARMOR_SIGS = [
  /luarmor\.net|Luarmor/i,
  /\bluraph_runtime1\s*\(/,
  /GetTutorialState\(\s*"nil\s+nil\s+/,
  /getfenv\(\)\s*\[\s*tbl\w+\s*\]\s*=/,
  /writefile\(\s*"luarmor-error-log\.txt"/,
  /[a-z]{1,3}\d-roblox-auth\.luarmor\.net/,
  /ce_like_loadstring_fn/,
  /Kick\(\s*"\[Luarmor\]/,
];

function detectLuarmor(source) {
  let hits = 0;
  for (const rx of LUARMOR_SIGS) {
    if (rx.test(source)) hits += 1;
  }
  if (hits >= 4) return 1.0;
  if (hits >= 3) return 0.9;
  if (hits >= 2) return 0.7; // single-sig canaries live below the 0.5 gate
  return 0.0;
}

const PLUGINS = [
  {
    name: 'luraph_v15',
    label: 'Luraph v15',
    detect: detectLuraph,
  },
  {
    name: 'luarmor_client',
    label: 'Luarmor whitelist client (loader; payload not devirtualizable)',
    detect: detectLuarmor,
  },
];

function detect(source) {
  let best = { plugin: null, confidence: 0 };
  for (const p of PLUGINS) {
    const c = p.detect(source);
    if (c > best.confidence) best = { plugin: p, confidence: c };
  }
  if (best.confidence < 0.5) {
    return { plugin: { name: 'generic', label: 'unknown obfuscator (behaviour trace only)' }, confidence: 0 };
  }
  return { plugin: best.plugin, confidence: best.confidence };
}

function byName(name) {
  const p = PLUGINS.find(x => x.name === name);
  if (!p) throw new Error(`Unknown obfuscator '${name}' (known: ${PLUGINS.map(x => x.name).join(', ')})`);
  return p;
}

module.exports = { detect, byName, restoreHeaderNewline, PLUGINS };