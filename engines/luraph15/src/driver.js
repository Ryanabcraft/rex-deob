'use strict';

const fs = require('fs');
const path = require('path');
const vmmap = require('./vmmap');
const harness = require('./harness');
const trace = require('./traceout');
const tidy = require('./tidy');

// tools/plain_funcs.py -> pretty-printed non-virtualized closures ('' if none)
function plainFunctions(src) {
  try {
    const { spawnSync } = require('child_process');
    const py = require('./pyenv').getPythonBin();
    const tool = path.join(__dirname, '..', 'tools', 'plain_funcs.py');
    const r = spawnSync(py, [tool, harness.luauAst(), src], { encoding: 'latin1', timeout: 120000, maxBuffer: 1 << 28 });
    if (r.status !== 0 || !r.stdout.trim()) return '';
    process.stderr.write('[*] ' + (r.stdout.match(/^-- (\d+)/) || [, '?'])[1] + ' non-virtualized function(s) recovered as plain Luau\n');
    return r.stdout;
  } catch { return ''; }
}

const SPIN_CHECKS = 24;
const SPIN_ROUNDS = 3;     // how many times a spinning closure is disabled and the run retried

function patchChunk(src, tmpdir, chunkTag) {
  const p = path.join(tmpdir, `_chunk_${harness.chunkKey(src)}.luau`);
  fs.writeFileSync(p, src, 'latin1');
  try {
    return vmmap.patchEntries(src, p, chunkTag);
  } catch (e) {
    process.stderr.write(`[!] could not instrument chunk (${e.message})\n`);
    return src;
  } finally {
    try { fs.unlinkSync(p); } catch {}
  }
}

// --- live HTTP for loaders (v1.4) ------------------------------------------
// Many v15 scripts are loaders: they HttpGet a key / library from the web and
// decode the rest with it. The sandbox answers HttpGet with a fake body, so the
// decode crashes ("size out of range", "bootstrap is invalid"). When a run ends
// in a script error after HttpGet calls, the real bodies are downloaded HERE (in
// Node; the script itself never touches the network) and served to the next run
// through cfg.http_map. --no-fetch disables it; DEOB_FETCH_MAX bounds the count.
// (this block was lost in the v15.1 fork and is what makes loader scripts work)
const HTTP_RE = /\b(?:HttpGet|HttpGetAsync|GetAsync)\s*\(\s*"(https?:\/\/[^"\s]+)"/g;
const URL_FIELD_RE = /\bUrl\s*=\s*"(https?:\/\/[^"\s]+)"/g;
function requestedUrls(body) {
  const out = new Set();
  for (const re of [HTTP_RE, URL_FIELD_RE]) {
    re.lastIndex = 0;
    let m;
    while ((m = re.exec(body))) out.add(m[1].replace(/\\(["\\\/])/g, '$1'));
  }
  return [...out];
}
async function fetchBody(url) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), 20000);
  try {
    const r = await fetch(url, { signal: ctl.signal, redirect: 'follow',
      headers: { 'User-Agent': 'Roblox/WinInet' } });
    const buf = Buffer.from(await r.arrayBuffer());
    if (buf.length > 8 * 1024 * 1024) return null;
    return { status: r.status, body: buf.toString('latin1') };
  } catch (e) {
    return null;
  } finally {
    clearTimeout(t);
  }
}
// Downloaded loader bodies stay on disk: a rerun of the same script, a retry after
// a crash or another user's copy must not hit the network again -- and an endpoint
// that has since died still answers from the cache. DEOB_HTTP_CACHE overrides dir.
const HTTP_CACHE_DIR = process.env.DEOB_HTTP_CACHE ||
  path.join(require('os').homedir(), '.deob_http_cache');
const HTTP_CACHE_MAX = parseInt(process.env.DEOB_HTTP_CACHE_MAX || '300', 10);
const httpCacheKey = (url) => require('crypto').createHash('sha1').update(url).digest('hex');
function httpCacheGet(url) {
  try {
    const ent = JSON.parse(fs.readFileSync(path.join(HTTP_CACHE_DIR, httpCacheKey(url) + '.json'), 'utf8'));
    return ent && typeof ent.body === 'string' ? { status: ent.status, body: ent.body } : null;
  } catch (e) { return null; }
}
function httpCachePut(url, res) {
  try {
    fs.mkdirSync(HTTP_CACHE_DIR, { recursive: true });
    const files = fs.readdirSync(HTTP_CACHE_DIR).filter((f) => f.endsWith('.json'));
    if (files.length >= HTTP_CACHE_MAX) {
      files.slice(0, files.length - HTTP_CACHE_MAX + 1)
        .forEach((f) => { try { fs.unlinkSync(path.join(HTTP_CACHE_DIR, f)); } catch (e) {} });
    }
    fs.writeFileSync(path.join(HTTP_CACHE_DIR, httpCacheKey(url) + '.json'),
      JSON.stringify({ url, status: res.status, body: res.body }), 'utf8');
  } catch (e) {}
}
const fetchedLog = [];
async function fetchMissing(body, httpMap, args) {
  if (args.noFetch) return 0;
  if (!/-- run status: script error/.test(body)) return 0;
  const max = parseInt(process.env.DEOB_FETCH_MAX || '16', 10);
  let added = 0;
  for (const url of requestedUrls(body)) {
    if (httpMap[url] || httpMap['GET ' + url] || fetchedLog.length >= max) continue;
    const cached = httpCacheGet(url);
    if (cached) {
      process.stderr.write(`[*] script needs ${url}: served from the download cache (${cached.body.length} bytes)\n`);
      httpMap[url] = cached;
      added++;
      continue;
    }
    const res = await fetchBody(url);
    fetchedLog.push(url);
    if (!res) { process.stderr.write(`[!] could not download ${url}\n`); httpMap[url] = { status: 404, body: '' }; continue; }
    process.stderr.write(`[*] script needs ${url}: downloaded ${res.body.length} bytes (HTTP ${res.status}), re-running with the real answer\n`);
    httpMap[url] = res;
    httpCachePut(url, res);
    added++;
  }
  return added;
}

async function run(job) {
  const { args } = job;
  const devirtOn = !args.noDevirt;
  let source = job.source;

  let patched;
  try {
    patched = args.noHooks ? source : vmmap.patchEntries(source, job.sourcePath, harness.chunkKey(source));
  } catch (e) {
    process.stderr.write(`[!] AST parse failed: ${e.message}\n`);
    throw e;
  }

  let spin = true;
  if (spin) patched = vmmap.patchSpin(patched);

  const cachePath = harness.loadP2dCache(job.input);
  const runner = new harness.Runner(job);

  const skip = [];
  let spins = 0;
  const chunks = {};
  const rawChunks = {};
  let body = null;
  let trapped = null;
  let cfg = null;
  const httpMap = Object.assign({}, (args.cfgJson && args.cfgJson.http_map) || {});

  for (let attempt = 1; attempt <= args.maxRuns; attempt++) {
    cfg = {
      time_budget: args.budget,
      dump_strings: args.strings,
      executor: args.executor,
      skip_protos: skip,
      devirt: devirtOn,
    };
    if (args.inputText) cfg.input_text = args.inputText;
    if (args.preludeFile) cfg.prelude = require('fs').readFileSync(args.preludeFile, 'latin1');
    if (args.noFold) cfg.fold = false;
    if (spin) cfg.spin = SPIN_CHECKS;
    if (args.cfgJson) Object.assign(cfg, args.cfgJson);
    if (Object.keys(httpMap).length) cfg.http_map = httpMap;

    process.stderr.write(`[*] tracing ${job.input} (run ${attempt})...\n`);
    const res = await runner.run(patched, cfg, chunks);
    body = res.body;

    if (!body) {
      runner.finish();
      throw new Error(res.err || 'Trace failed without output');
    }

    body = harness.takeP2d(body);

    const { chunks: found, body: cleanBody } = harness.takeChunks(body);
    body = cleanBody;
    let added = 0;
    for (const [key, src] of found) {
      if (!chunks[key]) {
        rawChunks[key] = src;
        chunks[key] = patchChunk(src, job.outdir, key);
        if (spin) chunks[key] = vmmap.patchSpin(chunks[key]);
        added++;
      }
    }
    if (added > 0) {
      process.stderr.write(`[*] script loadstring'd ${added} new VM chunk(s); instrumenting and re-running\n`);
      continue;
    }
    if (attempt < args.maxRuns && (await fetchMissing(body, httpMap, args)) > 0) continue;

    const trig = new RegExp(harness.mark('TRIGGER ') + '(\\d+)').exec(body);
    if (trapped !== null && trace.stmtCount(body) < trace.stmtCount(trapped[0])) {
      process.stderr.write(`[*] disabling function #${skip[skip.length - 1]} made script stop earlier: keeping run ${attempt - 1}\n`);
      body = trapped[0];
      skip.pop();
      break;
    }
    if (!trig) {
      // a run aborted in a loop still names the closure that was running: the
      // marker goes out on the raw stdout, outside the trace block. Disabling
      // that closure and re-running recovers what comes after it. A loop inside
      // the VM's own dispatch has no closure to skip (the marker prints nil) and
      // the truncated trace is kept as it is.
      const sp = new RegExp(harness.mark('SPIN ') + '(\\d+)').exec(harness.getLastRaw() || '');
      if (sp) {
        const spid = parseInt(sp[1], 10);
        if (!skip.includes(spid) && spins < SPIN_ROUNDS) {
          spins++;
          skip.push(spid);
          process.stderr.write('[*] endless loop inside function #' + spid
            + '; disabling it and re-running (' + spins + '/' + SPIN_ROUNDS + ')\n');
          continue;
        }
      }
      break;
    }

    trapped = [body, harness.getLastRaw()];
    const pid = parseInt(trig[1], 10);
    if (skip.includes(pid)) {
      process.stderr.write(`[!] anti-tamper trigger ${pid} fired again; giving up on reruns\n`);
      break;
    }
    process.stderr.write(`[*] anti-tamper trap reached through function #${pid}; disabling it and re-running\n`);
    skip.push(pid);
  }

  const runText = harness.traceText(body);
  body = harness.p2dMiss(body, cachePath);
  body = body.replace(new RegExp(harness.mark('TRIGGER ') + '\\d+\\n?', 'g'), '');

  const [protosJson, b1] = trace.takeLine(body, 'PROTOS');
  body = b1;
  const [force, b2] = trace.takeLine(body, 'FORCE');
  body = b2;
  if (force && devirtOn) process.stderr.write(`[*] constants decoded on request: ${force}\n`);

  const [unscrambled, b3] = trace.takeLine(body, 'UNSCRAMBLED');
  body = b3;
  if (unscrambled && devirtOn)
    process.stderr.write(`[*] ${unscrambled} function(s) scrambled by LPH_CRASH(): dumped as created\n`);

  const [b4, strings] = trace.takeStrings(body);
  body = b4;

  const notes = skip.length ? [`anti-tamper trap functions disabled: ${skip.map(p => '#' + p).join(', ')}`] : [];
  const text = trace.header(job.input, notes) + body;

  function writeTrace(withPlain) {
    let out = tidy.tidy(text, { preamble: !args.keepPreamble });
    if (withPlain) {
      // nothing ran virtualized: the functions Luraph left in plain Luau
      // (LPH_NO_VIRTUALIZE) are often the whole readable script (stage-2
      // chunks called as `setmetatable({...},{}):X(...)`)
      const plain = plainFunctions(job.sourcePath || job.input);
      if (plain) out = out.replace(/\s*$/, '\n\n') + plain + '\n';
    }
    job.write(job.tracePath, out);
  }

  if (!devirtOn && !job.debug) {
    writeTrace();
  }

  if (strings) job.write(job.path('.strings.txt'), strings);

  const dpath = job.path('.devirt.luau');
  if (protosJson) {
    const ppath = job.path('.protos.json');
    if (protosJson.startsWith('error:')) {
      process.stderr.write(`[!] proto capture failed: ${protosJson}\n`);
    } else {
      job.write(ppath, protosJson);
      if (devirtOn) {
        const chunkPaths = Object.entries(rawChunks).map(([k, src]) =>
          job.write(job.path(`.chunk_${k}.luau`), src, 'latin1')
        );

        const cfgData = {
          input: job.input,
          source: job.source,
          source_path: job.sourcePath,
          trace_path: job.tracePath,
          debug: job.debug,
          obfuscator: job.obfuscator,
          luau_exe: runner.luau,
          patched: patched,
          cfg: cfg,
          chunks: chunks,
          run_text: runText,
          ppath: ppath,
          dpath: dpath,
          chunk_paths: chunkPaths,
          args: {
            budget: args.budget,
            timeout: args.timeout,
            devirt_rounds: args.devirtRounds || 200,
            studio: false,
            no_fold: args.noFold || false,
            strings: args.strings || false,
            executor: args.executor || 'Wave',
          },
        };
        // Python stage: several candidates in parallel (PyPy + fixed hash seeds),
        // best result kept -- see src/parallel.js. DEOB_KEEP_CFG=1 keeps the inputs.
        await require('./parallel').runCandidates(cfgData, job);
      }
    }
  }

  runner.finish();
  trace.statusLine(body);

  if (devirtOn && fs.existsSync(dpath)) {
    const lifted = fs.readFileSync(dpath, 'utf8');
    if (!lifted.trim()) {
      // 0 functions lifted: the VM never ran virtualized code of its own (typical
      // for loaders whose real payload is fetched with HttpGet + loadstring). The
      // behaviour trace is the faithful result here -- never ship an empty file.
      process.stderr.write('[!] devirtualization lifted 0 functions (loader / remote payload?); writing behaviour trace instead\n');
      if (!job.debug) { try { fs.unlinkSync(dpath); } catch {} }
      writeTrace(true);
      return job.tracePath;
    }
    const nilCalls = (lifted.match(/\(nil\)\(/g) || []).length;
    const lines = lifted.split('\n').length;
    if (nilCalls < 50 || nilCalls * 100 < lines) {
      writeReport(job, dpath, 'lift');
      return dpath;
    }
    process.stderr.write(`[!] the devirtualized output is broken (${nilCalls} calls of nil); writing behaviour trace instead\n`);
    if (!job.debug) { try { fs.unlinkSync(dpath); } catch {} }
    writeTrace();
    return job.tracePath;
  }

  if (devirtOn) {
    process.stderr.write('[!] devirtualization produced no output; writing behaviour trace\n');
    if (!fs.existsSync(job.tracePath)) writeTrace();
  }
  return job.tracePath;
}

// One report per job, for the bot's verdict, the bench and the logs. The python
// lift writes a richer one (rounds, constants requested/cached) when it runs the
// single-run path; this fills the gap for the parallel-candidate path, which only
// keeps the winning text. Never overwrites the richer report.
function writeReport(job, path, mode) {
  try {
    const rep = path + '.report.json';
    if (fs.existsSync(rep)) return;
    const text = fs.existsSync(path) ? fs.readFileSync(path, 'utf8') : '';
    fs.writeFileSync(rep, JSON.stringify({
      engine: 'v15.1',
      mode,
      functions: (text.match(/^\s*(?:local\s+)?function\b/gm) || []).length,
      lines: text ? text.split('\n').length : 0,
      bytes: text.length,
      nil_calls: (text.match(/\(nil\)\(/g) || []).length,
      unlifted_blocks: (text.match(/error\("devirt:/g) || []).length,
      undecoded_constants: (text.match(/constant not decoded/g) || []).length,
      budget: job.args.budget,
      timeout: job.args.timeout,
    }, null, 1));
  } catch (e) {}
}

async function runGeneric(job) {
  const { args } = job;
  const cachePath = harness.loadP2dCache(job.input);
  const runner = new harness.Runner(job);
  const cfg = {
    time_budget: args.budget,
    executor: args.executor,
    dump_strings: args.strings,
  };
  if (args.inputText) cfg.input_text = args.inputText;
  if (args.noFold) cfg.fold = false;
  if (args.cfgJson) Object.assign(cfg, args.cfgJson);

  process.stderr.write(`[*] tracing ${job.input}...\n`);
  const res = await runner.run(job.source, cfg);
  if (!res.body) {
    runner.finish();
    throw new Error(res.err || 'Trace failed without output');
  }
  runner.finish();
  let body = harness.takeP2d(res.body);
  body = harness.p2dMiss(body, cachePath);
  const { body: cleanBody } = harness.takeChunks(body);
  body = cleanBody;
  const [b2, strings] = trace.takeStrings(body);
  body = b2;

  const text = trace.header(job.input) + body;
  job.write(job.tracePath, tidy.tidy(text, { preamble: false }));
  if (strings) job.write(job.path('.strings.txt'), strings);
  trace.statusLine(body);
  return job.tracePath;
}

module.exports = { run, runGeneric };