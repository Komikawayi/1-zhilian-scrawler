# 智联招聘 EdgeOne 风控逆向分析全记录

> 目标：智联招聘 (zhaopin.com) 职位搜索 + 职位详情，实现**浏览器无关的本地纯协议采集器**，还原前端加密算法协议。
> 日期：2026-08-06 起
> 环境：Windows 11 / Python 3.14 / Node v24 / curl_cffi 0.16 / CloakBrowser (js-reverse MCP)

---

## 0. 结论速览

| 项 | 结论 |
|----|------|
| **访问门控** | 腾讯云 **EdgeOne**，按 TLS/HTTP2 指纹 (JA3/JA4) + IP 信誉分级 |
| **搜索页数据** | SSR HTML `__INITIAL_STATE__.positionList`，`curl_cffi impersonate=chrome` 直接可取 |
| **详情页数据** | **推荐 `position-detailv2` JSON API（纯协议无挑战，20/20 实测）**；SSR 内联 `__INITIAL_STATE__.jobDetail`（detailedPosition 72 字段 + detailedCompany 24 字段）为备选，但首跳有 EdgeOne JS Challenge |
| **详情页主防线** | **JS Challenge**（`EO-Bot-Js-Token` cookie），91-opcode 字节码 VM，**Node vm 沙箱直接执行即可，无需解混淆** |
| **详情页兜底防线** | **TCaptcha TDC** 交互验证码（`cap_union_prehandle`/`tdc.js`/POW），仅 IP 信誉极差触发，算法已从源码摸清 |
| **瑞数** | 搜索页加载瑞数脚本（`/4QbVtADbnLVIc/d.随机.js`，`$_ts` 特征），但与详情页 EdgeOne 是**两套独立技术** |
| **有价值 JSON 接口** | `fe-api.zhaopin.com` 系列：`search/base/data`(2.07MB 筛选字典)、`city-page/user-city`、`experiment/config/initialize`、**`jobs/position-detailv2`(职位详情)**、`jobs/qrcode`、`user/unread-message` |
| **fe-api 动态参数** | `_v` + `x-zp-page-request-id` + `x-zp-client-id` **非签名**，服务端不校验，随机生成即可 |

**最终交付形态**：纯 Python 采集器（`curl_cffi`）+ 一个 Node vm 挑战执行器（`tools/eo_solve.js`），无浏览器、无 Playwright/Selenium。

---

## 1. 侦察环境与方法论

### 1.1 工具链

- **skill 工作流**：`web-protocol-recovery`（端到端协议恢复，startup gate → 五阶段 loop）、`trace`（CAPTCHA/TDC 协议链，`references/captcha/tencent-edgeone-tdc-workflow.md` 是腾讯 EdgeOne/TCaptcha 专用工作流）、`env-patch`（Node 补环境）、`ast-deobfuscate`（AST 解混淆）
- **浏览器/MCP**：CloakBrowser（`npx js-reverse-mcp --cloak`）做 CDP 网络/源码/断点证据；`cloakbrowser` 库脚本做页面级侦察（用户早期）
- **HTTP**：`curl_cffi`（Chrome TLS 指纹）、原生 `requests`（对照组）
- **运行时**：Node v24（vm 沙箱执行 challenge JS）、Python 3.14

### 1.2 方法要点

- **先取干净基线再上 hook**（观察者效应规则）
- **信线路不信页面文字**：搜索页加载了瑞数脚本，但真正卡详情页的是 EdgeOne JS Challenge
- **Cookie 出处要证明**：curl_cffi 在 HTTP/2 下 Set-Cookie 解析异常是一个大坑（见 §6.3）
- **防线分级要实测**：不同 IP 信誉 / 不同客户端指纹，返回不同防线

---

## 2. 搜索页链路（Phase 1-5，历史成果）

### 2.1 真实请求路径

```
sou.zhaopin.com/?jl=530&kw=python&p=1
   │  EdgeOne 校验 TLS/HTTP2 指纹
   ▼  302 重定向 (服务端完成 kw 编码)
www.zhaopin.com/sou/jl530/kw01O00U80EG06G03F01N0/p1
   │  SSR HTML (__INITIAL_STATE__.positionList 含 20 条职位)
   ▼
纯 HTML 解析即可取数, 无需额外 JSON 接口
```

- **kw 编码**（`python→01O00U80EG06G03F01N0`）在服务端 302 完成，客户端跟随重定向即可
- **EdgeOne 门控**：按 TLS 指纹判断，`curl_cffi impersonate=chrome` 是唯一且充分的绕过
- 注入浏览器 cookie 仍会被拦 → 证明是指纹判断而非 cookie

### 2.2 结论

搜索页采集 = `curl_cffi chrome` + SSR 解析，稳定可跑（多关键词/多城市/翻页验证过）。

---

## 3. 详情页链路还原（Phase 6，本轮核心）

### 3.1 现象

`curl_cffi` 访问 `www.zhaopin.com/jobdetail/{positionId}.htm`：

| 客户端 | 结果 |
|--------|------|
| `curl_cffi impersonate=chrome`（HTTP/2） | 稳定返回 **29KB JS Challenge 壳** |
| 原生 `requests`（HTTP/1.1 普通 TLS） | IP 信誉好时直接数据，差时**交互验证码** |
| CloakBrowser | 首次访问触发**交互验证码**（`captcha.eo.qq.com`） |

### 3.2 JS Challenge 壳结构（29KB）

首跳 HTML 是一个纯 `<script>` 挑战壳：

```javascript
window._aMYJ... = function (){return new Date()};            // 时间 hook
window._OPU... = function(a, b){return Date[a].apply(Date, b)};
var Qua7lMrVs39mmYCjI2s = function(){ /* 91-opcode 字节码 VM 解释器, case 0-91 */ };
Qua7lMrVs39mmYCjI2s("BBIIAE4gEkQg2qoBtp8...", false)
  (9672, [], window, [void 0, null, 0x67452301, 0x67452302, ..., 0x9e3779b9, ...], void 0)();
// VM 执行后定义:
//   window.solveChallenge(challenge, seed)
if(window.solveChallenge){
  var r = window.solveChallenge('C+mpzNMs...', 'Hw0m0RtgHFMMWGHH#34c435f890e3d8a4ff49e03b7fd7f335');
  if(r && r.token){
    document.cookie = "EO-Bot-Js-Token=" + r.token + "; path=/; max-age=3600; domain=.zhaopin.com";
    location.href = location.href.replace(/[?&]tads/,'');
  }
}
```

要点：
- VM 入参常量含 **SHA-256 K 常量**（`0x6b901122` 等）+ TEA delta（`0x9e3779b9`）→ 是某种哈希变体
- challenge 与 seed 都是**每次下发不同**的（首跳响应内嵌）
- token 格式：`local#<challenge>#<计算hash>`（683 字符）

### 3.3 求解方案：Node vm 最小沙箱直接执行

**关键决策**：不需要解混淆 VM，直接执行（`server-js-cookie-bootstrap` 模式的最快稳定策略）。

`tools/eo_solve.js`（最小沙箱）：

```javascript
const sandbox = {
  window: {},            // solveChallenge 会写到 window
  document: { cookie: '' },
  location: { href: 'https://www.zhaopin.com/jobdetail/xxx.htm', replace(){}, assign(){} },
  navigator: { userAgent: '...Chrome/146...', platform: 'Win32', language: 'zh-CN' },
  Date, Math, JSON, atob, btoa,
};
sandbox.window = sandbox;          // window 指向沙箱全局
vm.createContext(sandbox);
vm.runInContext(code, sandbox, { timeout: 15000 });
// 从 HTML 正则提取 solveChallenge('challenge','seed') 调用参数
const r = sandbox.window.solveChallenge(ch, seed);
// r = { token, args, answer, timestamp, isbypass }
```

调用结果：
```json
{ "token": "local#C+mpzNMs...#nykNwJsYMPtbTliEh8tu2e1m6Uu5uPg9LpRS9qNnPKmXWWC7SFLDBBEgvQHVYxPxKtd9t+...",
  "answer": 1427, "timestamp": 1785980500, "isbypass": 0 }
```

**一次真实重放成功**：带 `EO-Bot-Js-Token` cookie 重放 → 1.7MB 含 `__INITIAL_STATE__` 的真实数据。

### 3.4 重放链路（fetch_job_detail 流程）

```
1. GET 详情页 (无 cookie)
   ├─ 返回 __INITIAL_STATE__+jobDetail  → 直接成功
   ├─ 返回 JS Challenge 壳              → 提取 script → Node 求解 token → 带 cookie 重放
   │    重放: __INITIAL_STATE__ → 成功
   │    重放: Security Verification → IP 已升级, 需冷却/报错
   └─ 返回 Security Verification        → IP 已升级, 冷却重试或报错
```

---

## 4. EdgeOne 防线分级（重要经验）

实测同一 IP 不同客户端/频率，EdgeOne 返回不同防线：

| 防线 | 形态 | 触发条件 | 绕过 |
|------|------|----------|------|
| 直接放行 | 1.7MB 真实数据 | IP 信誉好 + Chrome 指纹 | — |
| **JS Challenge** | 29KB VM 壳 | 无 cookie 访问详情页 | Node vm 执行（已还原）|
| **交互验证码** | 1.9KB `Security Verification` + `cap_union_prehandle` | IP 信誉极差（连续高频）| TCaptcha TDC 链（复杂）|

关键事实：
- **curl_cffi Chrome 指纹信誉稳定**：连续 15 次/0.4s 也只触发 JS Challenge，不升级验证码
- **requests 无指纹**：IP 信誉差时首跳直接交互验证码
- **IP 信誉是共享资源**：成功通过几次后 EdgeOne 会放行该 IP（临时窗口）；但大量失败/高频会升级到验证码，需冷却（5-30 分钟）
- 成功过一次后干净 session 首跳也可能直接放行（EdgeOne 记住了 IP 信誉）

### 验证码放行绑定浏览器指纹（重要边界，实测确认）

IP 信誉差时触发交互验证码，用户手动过人机验证后：

- **CloakBrowser（真实浏览器）**：验证码通过后**能**访问详情页（`EO-Bot-Captcha-Token` 写入会话）
- **curl_cffi（Chrome 指纹模拟）**：即使带浏览器算的 `EO-Bot-Js-Token` + `EO-Bot-Captcha-Token`，重放**仍返回验证码**

诊断对照（同一轮内）：
| 重放请求 | 响应 |
|----------|------|
| 无 token | challenge 壳 |
| 垃圾 token | challenge 壳（token 无效 → 重新挑战）|
| **有效 token（Node 算的）** | **交互验证码**（token 通过 → 但 IP/指纹不被信任，升级）|
| 有效 token + Captcha-Token（浏览器产物）| 交互验证码（**指纹不匹配**）|

**结论**：
- `EO-Bot-Js-Token` 计算**完全正确**（垃圾 token 返回 challenge 证明它被读取校验）
- 但 EdgeOne 的验证码放行**绑定浏览器会话/指纹**（JA3/JA4 + 会话 cookie），curl_cffi 无法复用验证码产物
- **详情页纯协议采集的前提是 IP 信誉足够好**（首次 e2e 用 curl_cffi + Js-Token 拿到 1.7MB 已证明）；IP 被标记后只能冷却或换 IP，纯协议无法强行绕过验证码

这与 51job-crawler 的结论一致：**真实瓶颈是 IP 风控，不是逆向算法**。采集器应内置强风控纪律（低频、限并发、触发验证码即停），而非每次硬闯验证码。

---

## 5. 有价值接口（fe-api）

`fe-api.zhaopin.com/c/i/*`，**全部可用 curl_cffi 纯协议调用，无需签名**：

| 接口 | 内容 | 备注 |
|------|------|------|
| `search/base/data` | 2.07MB 筛选字典（公司类型/地铁线/行政区/学历/经验等） | 浏览器抓 2.18MB，纯协议 2.07MB |
| `city-page/user-city` | IP 城市定位 | 参数 `ipCity`/`ipProvince`/`userDesiredCity` |
| `experiment/config/initialize` | 实验配置开关 | |
| `jobs/qrcode` | 职位二维码 | `number` + `width` |
| `user/unread-message` | 未读消息 | 未登录返回 code=210 |

动态参数 `_v`（随机小数）+ `x-zp-page-request-id`（UUID+ts）+ `x-zp-client-id`（UUID）：**随机/无参数/浏览器真实参数三种都返回 200** → 非签名，仅追踪标识。

headers 固定：`x-zp-business-system:1`、`x-zp-page-code:4019`、`x-zp-platform:13`。

---

## 6. TCaptcha TDC 链（兜底防线，源码级分析）

严格按 `trace/references/captcha/tencent-edgeone-tdc-workflow.md` 分析。组件源码已下载：`TEOCaptchaWidget.js` / `tcaptcha_widget_eo_frame.js` / `widget_ele.js` / `dy_jy.js`。

### 6.1 协议链

```
可选首跳 protected page
  → cap_union_prehandle   (拿 sess/sid/pow_cfg/tdc_path/dyn_show_info)
  → 下载动态 tdc.js       (refreshDyJs {src: tdc_path})
  → 构造 iframe 环境      (TCaptchaSid/TCaptchaReferrer/TCaptchaIframeClientPos/window.name)
  → TDC.setData 序列:
       setData({isNewEntry:1})
       setData(trackerPayload)          # slideValue/verifyBtnPos/opAreaPos/clientSize
       setData({ft})                    # ft = widget webpack 模块 o["default"]()
       setData("autoVerify", [{elem_id:0, type:"DynAnswerType_TIME", data:""}])   # click_verify 分支
  → collect = decodeURIComponent(TDC.getData(true) || "")
  → eks = (TDC.getInfo() || {}).info
  → POW: 找 nonce 使 md5(prefix + decimal_nonce) == pow_cfg.md5
  → cap_union_new_verify 表单:
       collect + tlg(=collect 长度) + eks + sess + ans(DynAnswerType_TIME) + pow_answer + pow_calc_time
  → 成功判定: errorCode=="0" && ticket 非空 && randstr 非空
```

### 6.2 POW 算法（已从源码确认）

`widget_ele.js` 内嵌 getWorkloadResult：

```javascript
getWorkloadResult = function(t, n) {
  for (var e = t.nonce, r = t.target, o = +new Date, u = 0,
           f = "number" == typeof n ? n : 30000;
       md5("" + e + u) !== r && (u += 1, !(+new Date - o > f)););
  return { ans: u, duration: +new Date - o };
}
```

即：**`md5(prefix + decimal_nonce) == pow_cfg.md5`**，找到匹配的十进制 nonce `u`。md5 是组件内嵌的自包含实现。

### 6.3 curl_cffi 的 Set-Cookie 解析坑（重要）

- curl_cffi 0.16 在 HTTP/2 下 `r.headers['set-cookie']` 只返回 `'path=/'`，`r.cookies` 为空 → **拿不到首跳的会话 cookie**
- 原生 requests（HTTP/1.1）能正确拿到：`acw_tc`、`cdn_sec_tc`（HttpOnly, 3600s）、`x-zp-client-id`
- **结论**：详情页重放只需带 `EO-Bot-Js-Token`（首次真实重放成功即证明）；`acw_tc` 等非必需。但若将来需要，需用 requests 或 curl 原始头解析

### 6.4 触发与捕获

- **当前未触发**：curl_cffi 高频 15 次也只到 JS Challenge 层；TDC 需 IP 信誉极差或特定指纹
- **已保存组件源码** → 后续触发时按 workflow 捕获同轮证据（prehandle 响应/tdc.js/setData 序列）落地 iv8 重建

### 6.5 TCaptcha TDC 协议还原进展（zhilian-detail-tdc-002，2026-08-07）

独立构造的 EO widget 页（`appid=25200697`）确定性触发验证码，已打通纯协议链路：

**prehandle（独立 HTTP，无 cookie）**
```
GET https://captcha.eo.qq.com/cap_union_prehandle
  ?aid=25200697 & protocol=https & accver=1 & showtype=inline & lang=zh-cn & fb=1
```
返回：`sess`（fresh）、`sid=1343190090`、`data.comm_captcha_cfg.tdc_path`、`data.dyn_show_info`。
- **`show_type="click_verify"`**（复选框，"确认您是真人"）—— 匹配 workflow 已验证的 click_verify 分支
- **`pow_cfg=null`** —— 本挑战类型**无需 POW**

**动态 tdc.js**：192-195KB，VM 混淆（obfuscator.io 字符串数组 + 字节码 VM），定义
`window.TDC = {getInfo, setData, clearTc, getData}`、`window.TDC_NAME`、`window[TDC_NAME]`（352 字符 = eks）。

**Node vm 重建（tdc_solve.js）**：补全浏览器环境（UA-CH/screen/performance/DOM）后 tdc.js 完整运行，
`setData({isNewEntry}) → setData({slideValue...}) → setData({ft})` → `getData(true)`=collect（2800-3100 字符）、
`getInfo().info`=eks。**ft 生产者已提取**（widget 模块 48：50 项特性检测 bit-packed base64url）。

**verify 表单**：`collect/tlg/eks/sess/ans[/deviceID][/pow][/vData]`，`ans=JSON.stringify(dataManager.getData())`
= `[{"elem_id":0,"type":"DynAnswerType_TIME","data":""}]`。errorCode 语义：0=成功、9=verifyFailRefresh、12=verifyError。

**当前卡点**：`cap_union_new_verify` 返回 errorCode 9/12（未到 0）。无 ans→9、带 ans→12。ft/deviceID 非决定因素，
**根因大概率是 Node 沙箱的 collect 环境指纹与真实 Chrome 不一致**。下一步：捕获一轮浏览器真实 verify（人工过验证）
作为纯协议对照，重放验证 + 定位指纹差异。详见 `js_reverse_cache/tasks/zhilian-detail-tdc-002/report.md`。

---

## 6.6 position-detailv2：绕过 EdgeOne 的职位详情 JSON API（本轮重大发现）

从详情页 JS bundle（`jobdetail._code_.web.*.js`）挖出 fe-api 端点清单，其中 **`/c/i/jobs/position-detailv2`** 是职位详情 JSON API，**纯协议直接可调，完全无需挑战/Node/验证码**。

### 调用契约

```
GET fe-api.zhaopin.com/c/i/jobs/position-detailv2
  ?number=<职位号>            # 搜索页 positionList.number 直接可用
  &_v=<随机小数>
  &x-zp-page-request-id=<uuid-ts>
  &x-zp-client-id=<uuid>
  &platform=13&version=0.0.0
headers: x-zp-business-system:1 / x-zp-page-code:4019 / x-zp-platform:13
```

### 实测数据

- **20/20 连续成功**（0.15s 间隔），0 challenge 事件、0 验证码、无 IP 信誉依赖
- 响应 `code=200` + `apiCode=200`，`data = { detailedPosition(67字段), detailedCompany(22字段), taskId }`
- 字段完整：`positionName` / `salary60` / `positionWorkingExp` / `education` / `positionCityDistrict` / `jobDesc`（完整 462 字符岗位描述）/ `companyName` / `companySize` 等
- **比 SSR + 挑战路径更快更稳**：5KB JSON vs 1.7MB SSR；无 Node 依赖；不受详情页 IP 信誉限制

### 对采集方案的影响

```
旧方案 (SSR + 挑战):  GET /jobdetail/{id}.htm -> JS Challenge -> Node 求解 -> 重放 -> 1.7MB SSR
                        依赖 IP 信誉, IP 差时触发验证码 (绑定浏览器指纹, 纯协议无法绕过)

新方案 (推荐, v2 API): 搜索 -> SSR positionList.number -> position-detailv2?number= -> 详情 JSON
                        纯协议全程, 无挑战/验证码/IP 信誉依赖 (20/20 实测)
```

**结论**：详情采集首选 `position-detailv2`。SSR + 挑战路径保留为兜底（`--detail-urls`），已还原的 JS Challenge 求解仍有效但不再是必需。

---

## 7. 采集器落地

```
zhilian-crawler/
├── main.py                    # CLI: 搜索采集(--kw/--jl) + 详情采集(--detail)
├── config/settings.py
├── utils/
│   ├── http_client.py         # ZhilianClient (curl_cffi chrome 指纹, 限速)
│   ├── fe_api.py              # fe-api 请求: fetch_position_detail_v2 (推荐, 无挑战)
│   ├── challenge.py           # SSR + EdgeOne JS Challenge 求解 (兜底)
│   ├── parser.py              # 搜索 positionList + 详情 v2/SSR 解析
│   └── output.py              # CSV (UTF-8 BOM)
├── tools/eo_solve.js          # Node vm 挑战执行器 (SSR 兜底路径用)
├── tests/
│   ├── test_parser.py
│   ├── test_protocol.py       # (网络测试, ZHAOPIN_NETWORK_TEST=1)
│   └── test_challenge.py      # 固定输入自检 (challenge 求解 + v2/SSR 详情解析) 11 项
└── js_reverse_cache/          # 侦察素材 + 任务证据
    ├── eo_solve.js / e2e_detail.py / probe_*.py
    ├── scripts/ (challenge JS 样本)
    ├── html/ (challenge 壳 / 详情数据样本)
    └── tasks/zhilian-detail-tdc-001/ (TDC 分析证据 + report.md)
```

用法：
```bash
# 搜索
python main.py --kw python --jl 530 --pages 3

# 详情 (推荐: position-detailv2 JSON API, 无需 Node/挑战)
python main.py --detail --detail-numbers "CCL1480117890J40614881205,CCL1467242830J40855145310" --output out.csv

# 详情 (兜底: SSR + EdgeOne JS Challenge, 需 Node)
python main.py --detail --detail-urls "https://www.zhaopin.com/jobdetail/xxx.htm" --output out.csv
# 触发验证码时冷却重试
python main.py --detail --detail-urls "..." --captcha-cooldown 60
```

---

## 8. 心得与经验（可复用）

### 8.1 方法论

1. **JS Challenge 类防线优先"直接执行"而非解混淆**：EdgeOne 的 91-opcode VM 只需 Node vm 沙箱执行，拿到 token 就算还原，不必理解每条 opcode。
2. **防线分级要先实测**：同一目标对 requests / curl_cffi / CloakBrowser 返回完全不同防线。curl_cffi 的 Chrome 指纹信誉最稳，只触发可解的 JS Challenge。
3. **IP 信誉是共享稀缺资源**：大量探测会耗尽信誉升级到验证码。测试要克制、要冷却、要在同一轮内把证据拿全（Same-Round State）。
4. **判断"签名还是追踪"用三组对照**：随机参数 / 无参数 / 浏览器真实参数都返回 200 → 非签名。
5. **Cookie 出处必须证明**：curl_cffi 的 Set-Cookie 解析异常导致"看着对实则缺 cookie"的假象，用 requests 对照才发现是客户端解析问题而非服务端差异。
6. **观察者效应真实存在**：CloakBrowser 打开详情页触发验证码，但 curl_cffi 同 IP 却只拿到 JS Challenge——工具本身改变采样。
7. **从页面 JS bundle 挖接口能绕过防线**：详情页 JSON 数据看似在 SSR（带挑战），但从 `jobdetail._code_.web.*.js` bundle 挖出 `position-detailv2` 纯协议 API 直接拿 JSON——**防线是页面路由的，不一定是数据的**。遇到难啃的页面防线，先搜它的 bundle 找等价 JSON API。

### 8.2 踩过的坑

- ❌ 以为重放失败是 token 无效 → 实际是 IP 信誉已升级到验证码（curl_cffi 16 次高频探测耗尽了）
- ❌ 用户过了人机验证后 curl_cffi 带浏览器 token 仍被拦 → 验证码放行**绑定浏览器指纹/会话**，curl_cffi 复用不了（不是 token 问题）
- ❌ curl_cffi `r.headers['set-cookie']` 只有 `path=/` → 以为是服务端不设 cookie，实为 HTTP/2 解析 bug
- ❌ 轻量验证码页不含 `cap_union_prehandle` 字符串 → `is_captcha_page` 最初判断失败，放宽为只查 `Security Verification`
- ❌ 用户口述"瑞数" → 实际搜索页有瑞数脚本，但详情页门控是**腾讯云 EdgeOne JS Challenge**，两套独立技术
- ❌ 一开始死磕 SSR + 挑战 → 从 bundle 挖出 position-detailv2 后发现完全不需要，**先搜等价 JSON API 再啃挑战**

### 8.3 工具选择经验

- **Node vm 沙箱**执行 challenge JS 是最小可行方案，jsdom 补环境反而引入 cross-realm 差异
- **curl_cffi** 是纯协议采集的主力；requests 只做 cookie/响应头对照
- **trace skill 的 tencent-edgeone-tdc-workflow.md** 是腾讯验证码链的标准参考，识别信号：`cap_union_prehandle`、`TDC.setData/getData/getInfo`、`pow_cfg`、`dyn_show_info`
- **详情采集首选 position-detailv2**（无挑战）；SSR + 挑战路径保留兜底

---

## 9. 遗留问题与待办

- [x] **详情采集端到端**：position-detailv2 纯协议 20/20 验证通过（推荐路径）
- [ ] **SSR + 挑战路径端到端复验**（需 IP 信誉恢复；首次 e2e 已证明可行）
- [ ] **TCaptcha TDC 完整落地**：捕获同轮 prehandle/tdc.js/setData 证据，iv8 重建 → collect/eks/POW → new_verify → ticket
- [ ] 挖掘更多有价值接口（`similar-positions-new` 相似职位、公司详情、简历相关）
- [ ] 采集器规模化前的风控策略（频率控制、token 复用窗口 3600s、验证码冷却自动退避）

---

*文档维护：后续每个逆向阶段结束都应更新本文档，遵循全局规则「逆向必写文档」([[reverse-engineering-docs]])。*
