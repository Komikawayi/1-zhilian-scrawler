#!/usr/bin/env node
/**
 * EdgeOne JS Challenge 最小沙箱执行器
 * 执行 challenge JS -> 计算 EO-Bot-Js-Token
 */
'use strict';
const fs = require('fs');
const vm = require('vm');

// ---- 最小环境存根 ----
let cookieJar = '';
const href = 'https://www.zhaopin.com/jobdetail/CCL1480117890J40614881205.htm?refcode=4019&srccode=401903&preactionid=d0503c99-611e-4c16-90bd-d5714357df5b';

const sandbox = {
  window: {},
  document: {
    cookie: '',
    getElementById: () => null,
    createElement: () => ({}),
  },
  location: {
    href: href,
    replace: function (u) { console.log('[nav] location.replace ->', String(u).slice(0, 80)); },
    assign: function (u) { console.log('[nav] location.assign ->', String(u).slice(0, 80)); },
  },
  navigator: {
    userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36',
    platform: 'Win32',
    language: 'zh-CN',
  },
  console,
  Date,
  Math,
  JSON,
  atob: (s) => Buffer.from(s, 'base64').toString('binary'),
  btoa: (s) => Buffer.from(s, 'binary').toString('base64'),
};
sandbox.window = sandbox; // window 指向全局沙箱
sandbox.globalThis = sandbox;

try {
  const code = fs.readFileSync(process.argv[2] || 'js_reverse_cache/scripts/eo_challenge_0.js', 'utf-8');
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox, { timeout: 15000 });

  if (typeof sandbox.window.solveChallenge === 'function') {
    const m = code.match(/solveChallenge\('([^']*)',\s*'([^']*)'\)/);
    if (m) {
      const r = sandbox.window.solveChallenge(m[1], m[2]);
      const out = {
        ok: true,
        token: (r && r.token) || null,
        answer: (r && r.answer) || null,
        timestamp: (r && r.timestamp) || null,
        isbypass: (r && r.isbypass) || null,
        cookie: sandbox.document.cookie || null,
      };
      console.log(JSON.stringify(out));
      process.exit(0);
    }
  }
  console.log(JSON.stringify({ ok: false, error: 'solveChallenge 未定义' }));
  process.exit(1);
} catch (e) {
  console.error(JSON.stringify({ ok: false, error: e.message }));
  process.exit(1);
}
