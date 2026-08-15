// graphview_perf.mjs — drive the console Graph View perf mode at 5,000 nodes.
//
// Reuses the graphview-three benchmark architecture (docs/bench results,
// 2026-08-13): serve the console static dir, open #/graph?perf=1 in a real
// browser (headed Chrome/Edge on the physical display is the gate number;
// headless SwiftShader is a lower bound, never the gate), and read
// window.__GRAPH_PERF after the 20 s decay-animation window.
//
// Playwright is resolved from the gitignored .bench/graphview-three install
// (node_modules), so the project venv stays free of browser deps.
//
// Run (CI-skip-safe: needs a display + GPU):
//   uv run --no-sync pytest tests/test_console_graph_perf.py -s
//   node scripts/graphview_perf.mjs

import { createRequire } from 'node:module';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const STATIC = path.join(ROOT, 'src', 'mnemoseed', 'console', 'static');
const PORT = Number(process.env.MNEMOSEED_PERF_PORT || 8433);
const BASE = `http://127.0.0.1:${PORT}`;

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'application/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.png': 'image/png',
};

// Minimal API stubs so the SPA's identity gate passes and the perf page can
// run fully client-side (the perf graph is synthetic; only the gate matters).
const API_STUBS = {
  '/api/v1/setup/status': { setup_required: false },
  '/api/v1/auth/me': { username: 'perf' },
  '/api/v1/status': { profiles: [{ profile_id: 'perf' }] },
};

const server = createServer(async (req, res) => {
  let p = decodeURIComponent(new URL(req.url, BASE).pathname);
  if (p.startsWith('/api/v1/')) {
    const body = JSON.stringify(API_STUBS[p] ?? {});
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(body);
    return;
  }
  if (p === '/') p = '/index.html';
  if (p.startsWith('/console')) p = p.slice('/console'.length) || '/index.html';
  const file = path.normalize(path.join(STATIC, p));
  if (!file.startsWith(STATIC)) {
    res.writeHead(403);
    res.end();
    return;
  }
  try {
    const body = await readFile(file);
    res.writeHead(200, { 'content-type': MIME[path.extname(file).toLowerCase()] || 'application/octet-stream' });
    res.end(body);
  } catch {
    res.writeHead(404, { 'content-type': 'text/plain' });
    res.end('not found: ' + p);
  }
});

// Playwright from the gitignored bench install (never a project dependency).
const benchModules = path.join(ROOT, '.bench', 'graphview-three', 'node_modules');
if (!existsSync(path.join(benchModules, 'playwright'))) {
  console.error(
    'bench playwright install not found under .bench/graphview-three/node_modules.\n' +
      'Set it up once (gitignored):  cd .bench/graphview-three && npm install'
  );
  process.exit(2);
}
const requireFromBench = createRequire(path.join(benchModules, 'noop.js'));
const { chromium } = requireFromBench('playwright');

const viewport = { width: 1600, height: 900 };

async function launchBrowser() {
  const modes = [
    { label: 'headed-chrome', options: { headless: false, channel: 'chrome', viewport, args: ['--window-size=1600,900'] } },
    { label: 'headed-edge', options: { headless: false, channel: 'msedge', viewport, args: ['--window-size=1600,900'] } },
    {
      label: 'headless-chromium-angle',
      options: { headless: true, viewport, args: ['--use-angle=default', '--enable-unsafe-swiftshader', '--window-size=1600,900'] },
    },
  ];
  for (const mode of modes) {
    try {
      const browser = await chromium.launch(mode.options);
      console.log(`launched: ${mode.label}`);
      return { browser, label: mode.label };
    } catch (e) {
      console.warn(`launch ${mode.label} failed: ${String(e.message).split('\n')[0]}`);
    }
  }
  throw new Error('no browser launch mode succeeded');
}

async function grabGpu(page) {
  return page.evaluate(() => {
    try {
      const canvas = document.createElement('canvas');
      const gl = canvas.getContext('webgl2') || canvas.getContext('webgl');
      if (!gl) return { webgl: false };
      const ext = gl.getExtension('WEBGL_debug_renderer_info');
      return {
        webgl: true,
        renderer: ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : null,
      };
    } catch (e) {
      return { webgl: false, error: String(e) };
    }
  });
}

function machineInfo() {
  const info = { cpu: os.cpus()[0]?.model || 'n/a', ramGb: +(os.totalmem() / 2 ** 30).toFixed(1), platform: `${os.platform()} ${os.release()}`, node: process.version };
  try {
    info.wmicGpu = execFileSync('wmic', ['path', 'win32_VideoController', 'get', 'name'], { encoding: 'utf8', timeout: 15000 })
      .split(/\r?\n/)
      .map((l) => l.trim())
      .filter(Boolean)
      .slice(1);
  } catch {
    /* wmic unavailable; the WebGL string is authoritative */
  }
  return info;
}

async function main() {
  server.listen(PORT, '127.0.0.1');
  console.log(`serving ${STATIC} on ${BASE}`);
  await new Promise((r) => server.once('listening', r));

  const { browser, label: browserMode } = await launchBrowser();
  const page = await browser.newPage();
  // Seed the SPA's stored session so the identity gate renders the app.
  await page.addInitScript(() => {
    localStorage.setItem('mnemoseed.token', 'perf-token');
    localStorage.setItem('mnemoseed.username', 'perf');
  });
  page.on('console', (msg) => {
    if (msg.type() === 'error' || msg.type() === 'warning') console.log(`[page.${msg.type()}] ${msg.text()}`);
  });
  page.on('pageerror', (err) => console.log(`[page.error] ${err.message}`));
  page.on('response', (res) => {
    if (res.status() >= 400) console.log(`[http ${res.status()}] ${res.url()}`);
  });
  try {
    // perf=1 must live in the query string (location.search), not the hash.
    await page.goto(`${BASE}/?perf=1#/graph`, { timeout: 30000 });
    // The perf window is 20 s after a warmup; give the page margin to finish.
    await page.waitForFunction(() => window.__GRAPH_PERF, undefined, { timeout: 60000, polling: 500 });
    const result = await page.evaluate(() => window.__GRAPH_PERF);
    const gpu = await grabGpu(page);
    console.log('result=' + JSON.stringify(result));
    console.log('gpu=' + JSON.stringify(gpu));
    console.log('machine=' + JSON.stringify(machineInfo()));
    console.log('browserMode=' + browserMode);
    if (result.avgFps < 30) {
      console.error(`FAIL: avg ${result.avgFps} fps < the 30 fps floor (NFR-7.2 v2)`);
      process.exitCode = 1;
    }
  } finally {
    await browser.close();
    server.close();
  }
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
