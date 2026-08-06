#!/usr/bin/env node
/**
 * EdgeOne JS Challenge 最小沙箱执行器
 * 执行 challenge JS -> 计算 EO-Bot-Js-Token
 *
 * 用法: node eo_solve.js <challenge.js> [href]
 *   challenge.js  首跳 challenge 壳的内联 <script> 内容
 *   href          (可选) 当前请求 URL, 供沙箱 location.href 使用
 */
'use strict';
const fs = require('fs');
const vm = require('vm');

const scriptPath = process.argv[2];
if (!scriptPath) {
  console.error('usage: node eo_solve.js <challenge.js> [href]');
  process.exit(1);
}

// ---- 最小环境存根 ----
const href = process.argv[3] || 'https://www.zhaopin.com/';

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
  const code = fs.readFileSync(scriptPath, 'utf-8');
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
