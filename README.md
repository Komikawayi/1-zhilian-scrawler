# zhilian no-proxy crawler

智联招聘逆向爬虫：搜索 SSR + 职位详情**纯协议**采集。
核心成果是完整破解腾讯 EdgeOne 职位详情页 **JS Challenge**（91-opcode 字节码 VM 本地执行算
`EO-Bot-Js-Token`），并摸清 TCaptcha TDC 兜底验证码链（`cap_union_prehandle` / `tdc.js` / POW）。

详情页挑战求解需 Node.js；采集全流程不依赖浏览器。侦察素材在 `js_reverse_cache/`，深度分析见
`docs/zhilian-edgeone-reverse-analysis.md`。

## 当前协议边界（逆向核心结论）

### 防线逆向状态

| 层 | 目标 | 状态 |
|----|------|------|
| ① 搜索访问门控 | EdgeOne TLS/HTTP2 指纹 (JA3/JA4) | ✅ 已破（`curl_cffi impersonate=chrome`）|
| ② 详情页主防线 | `EO-Bot-Js-Token`（91-opcode VM，29KB 挑战壳）| ✅ 已破（Node vm 求解，无需解混淆）|
| ②' 详情数据 | **`position-detailv2` JSON API（纯协议无挑战）** | ✅ **首选路径**（20/20 实测）|
| ③ 详情页兜底 | TCaptcha TDC（`cap_union_prehandle`/`tdc.js`/POW/`new_verify`）| 🔬 已摸清（源码级；待触发时捕获同轮证据）|
| ④ fe-api 动态参数 | `_v` / `x-zp-page-request-id` / `x-zp-client-id` | ✅ 非签名（随机/无参/真实参都 200）|

### 关键结论（实测）

- **搜索** `sou.zhaopin.com`：EdgeOne 按 TLS/HTTP2 指纹放行，`curl_cffi chrome` 直接过，302 后取 SSR
  `__INITIAL_STATE__.positionList`（20 条/页）。kw 编码在服务端完成，客户端无需自实现。
  多关键词/多城市/翻页 positionCount 稳定。
- **职位详情（推荐）** `position-detailv2` JSON API：`number` 用搜索页 `positionList.number`，
  纯协议直接拿 `{ detailedPosition(67字段), detailedCompany(22字段), taskId }`，**20/20 稳定、
  0 挑战、0 验证码、无 IP 信誉依赖**，`jobDesc` 完整。比 SSR 快（5KB vs 1.7MB）、无 Node 依赖。
- **职位详情（兜底）** `www.zhaopin.com/jobdetail/<id>.htm` SSR：无 cookie 首跳稳定返回 **29KB JS Challenge 壳**。
  内联 script 含 91-opcode VM + SHA 常量，执行后定义 `window.solveChallenge(challenge, seed)`，
  Node vm 沙箱直接执行得 token（`local#...`，683 字符，max-age 3600s），带 cookie 重放拿到
  `__INITIAL_STATE__.jobDetail`。**依赖 IP 信誉**——IP 被标记时验证码放行绑定浏览器指纹，纯协议无法绕过。
- **fe-api** `fe-api.zhaopin.com/c/i/*`：纯协议可调，无需签名。`search/base/data`（2.07MB 筛选字典）、
  `city-page/user-city`、`experiment/config/initialize`、`jobs/position-detailv2`、`jobs/qrcode`、
  `user/unread-message`。
- **真实瓶颈是 IP 信誉，不是逆向算法**：EdgeOne 按 IP 信誉分级——直接放行 → JS Challenge（可本地解）→
  交互验证码（`Security Verification`）。curl_cffi Chrome 指纹信誉稳（高频 15 次/0.4s 也不升级）；
  requests 无指纹在信誉差时首跳直接交互验证码。被升级后冷却 5-30 分钟恢复。

### 正确使用姿势

```
详情采集:   搜索拿 number → position-detailv2 (纯协议, 无需挑战/Node/验证码)
SSR 兜底:   --detail-urls 自动过 JS Challenge; IP 被标记时 --captcha-cooldown 60
```

## 架构

```text
CLI (main.py)
  -> 搜索模式 --kw <关键词> --jl <城市>
       sou.zhaopin.com 302 -> www.zhaopin.com/sou/{jl}{kw编码}/p{n}
       -> SSR __INITIAL_STATE__.positionList 解析 (含 number) -> CSV
  -> 详情模式 --detail
       --detail-numbers <num,>  [推荐] position-detailv2 JSON API (纯协议, 无挑战)
         GET fe-api/c/i/jobs/position-detailv2?number= -> 详情 JSON -> CSV
       --detail-urls <url,url>  [兜底] SSR + EdgeOne JS Challenge
         GET /jobdetail/{id}.htm -> 29KB challenge 壳
         -> Node vm 求解 EO-Bot-Js-Token -> 带 cookie 重放 -> SSR jobDetail -> CSV
         [IP 恶化] 交互验证码 -> --captcha-cooldown 冷却重试 或 报错
  -> 测试: pytest tests/ (固定输入自检, 不依赖网络)
```

关键模块：

```text
main.py                   CLI 入口 (搜索 + 详情 + 验证码冷却)
tools/login_collect.py    登录态采集 (简历/消息/投递/VIP/简历诊断)
utils/http_client.py      curl_cffi chrome 指纹客户端 (限速 1.2~2.5s/重试)
utils/fe_api.py           匿名接口 (position-detailv2) + 登录态接口 + 会话过期检测
utils/session.py          at/rt 登录会话加载/保存 (config/zhilian-session.local.json)
utils/device.py           deviceSn 纯协议生成/续期 (reportShuMeiDevice, 无需数美 SDK)
utils/challenge.py        fetch_job_detail: SSR + JS Challenge 求解 (兜底)
utils/parser.py           SSR 解析 (搜索 positionList + 详情 v2/SSR)
utils/output.py           CSV 输出 (UTF-8 BOM)
tools/eo_solve.js         Node vm 挑战执行器 (SSR 兜底路径用)
```

> 📄 风控机制分析（登录态/行为信号/心跳/请求节奏/数美）见 **[docs/zhilian-risk-control-analysis.md](docs/zhilian-risk-control-analysis.md)**。

## 登录态采集（需智联账号）

登录态经 URL 参数 `at`（access token）+ `rt`（refresh token）传递。捕获一次登录会话后纯协议采集：

```powershell
# 1. CloakBrowser 登录智联账号, 提取 cookie 里的 at/rt
# 2. 保存到 config/zhilian-session.local.json (gitignored):
#    {"at": "<32hex>", "rt": "<32hex>", "source": "..."}
# 3. 采集登录态数据
py tools/login_collect.py --output output/login_state.json
```

采集内容：简历列表、未读消息、投递/面试统计、VIP、简历诊断。会话过期（code=210）自动提示重捕获。


## 环境要求

- Python 3.11+（curl_cffi、requests）
- Node.js 24+（仅 SSR 兜底路径需要，`tools/eo_solve.js`；推荐 v2 路径不需要）
- CloakBrowser（仅侦察，`js-reverse MCP --cloak`；采集不依赖）

```powershell
pip install -r requirements.txt
```

## 采集

### 搜索采集

```powershell
py main.py --kw python --jl 530 --pages 3
py main.py --kw python,java,golang --city 北京 --pages 5 --output output/zhaopin.csv
```

### 职位详情采集

**推荐：position-detailv2 JSON API（纯协议无挑战，无需 Node）**

```powershell
# number 用搜索页 positionList.number
py main.py --detail --detail-numbers "CCL1480117890J40614881205,CCL1467242830J40855145310" --output output/zhaopin_detail.csv
```

**兜底：SSR + EdgeOne JS Challenge（需 Node.js）**

```powershell
py main.py --detail --detail-urls "https://www.zhaopin.com/jobdetail/CCL1480117890J40614881205.htm,https://www.zhaopin.com/jobdetail/CCL1467242830J40855145310.htm" --output output/zhaopin_detail.csv
```

触发交互验证码时自动冷却重试：

```powershell
py main.py --detail --detail-urls "..." --captcha-cooldown 60
```

### 测试

```powershell
py -m pytest tests/ -v                          # 固定输入自检 (challenge 求解 + v2/SSR 详情解析)
ZHAOPIN_NETWORK_TEST=1 py -m pytest tests/ -v   # 含真实网络测试
```

### 常用选项

```text
--kw <词>                 搜索关键词 (逗号分隔多个)
--jl <代码> / --city <名> 城市 (530=北京; 中文名也行)
--pages <n>               每个关键词页数
--detail                  详情采集模式
--detail-numbers <num,>   职位号列表 (推荐, position-detailv2 API 无挑战)
--detail-urls <url,url>   职位详情 URL 列表 (兜底, SSR + JS Challenge)
--captcha-cooldown <s>    触发交互验证码后的冷却秒数 (默认 0=直接报错)
--output <path>           CSV 输出路径
--sleep <s>               页面间随机等待基础值
```

## 运行文件

```text
output/zhaopin.csv          搜索采集结果
output/zhaopin_detail.csv   详情采集结果
js_reverse_cache/           逆向素材 (challenge 样本 / TDC 证据 / 报告)
js_reverse_cache/tasks/     任务证据目录 (zhilian-detail-tdc-001/)
docs/                       逆向分析文档
```

## 限速与风控纪律

- 搜索页 + `position-detailv2`（推荐详情路径）纯协议稳定，**不受详情页 IP 信誉影响**（20/20 实测）。
- SSR 兜底路径：详情页 challenge 自动本地求解，token 可复用 1 小时；**IP 信誉是共享资源**，
  大量探测会升级到交互验证码（验证码放行绑定浏览器指纹，curl_cffi 无法复用），需冷却 5-30 分钟。
- curl_cffi 0.16 在 HTTP/2 下 Set-Cookie 解析异常（`r.headers['set-cookie']` 只返回 `path=/`）；
  SSR 重放只需带 `EO-Bot-Js-Token`（首次重放 1.7MB 已验证），`acw_tc`/`cdn_sec_tc` 非必需。
- 逐条采集建议 1-2 秒/请求（`--sleep 1.5` 默认），高并发易触发风控。

## 失败策略

| 失败 | 结果 |
| --- | --- |
| 超时 / 连接重置 | 指数退避 + 重试（http_client 3 次）|
| position-detailv2 业务错误（code≠200）| 报错（含 message）|
| EdgeOne JS Challenge（SSR 详情首跳）| Node vm 本地求解 token → 重放 |
| 重放后仍 Challenge | 重试（默认 2 次）|
| 交互验证码 `Security Verification` | `--captcha-cooldown` 冷却重试；否则报错提示冷却 |
| `__INITIAL_STATE__` 缺失 / JSON 解析失败 | 报错 |

## 逆向研究命令

侦察与验证工具在 `js_reverse_cache/`（均为一次性研究脚本，不入采集主流程）：

```text
js_reverse_cache/probe_feapi.py        fe-api 纯协议对照 (随机/无参/真实参)
js_reverse_cache/e2e_detail.py         详情页 challenge 端到端验证
js_reverse_cache/probe_trigger.py      防线触发条件探测 (高频访问观察升级)
js_reverse_cache/test_replay_cookie.py requests vs curl_cffi 重放对照
```

详细逆向过程、防线分级、TDC 协议链与踩坑记录：
- `docs/zhilian-edgeone-reverse-analysis.md`
- `js_reverse_cache/tasks/zhilian-detail-tdc-001/report.md`

## 遗留工作

- [x] 详情采集端到端（position-detailv2 纯协议 20/20，推荐路径）
- [ ] SSR 兜底路径端到端复验（需 IP 信誉恢复；首次已证明可行）
- [ ] TCaptcha TDC iv8 重建（待 IP 触发时捕获同轮 prehandle/tdc.js/setData 证据）
- [ ] 更多有价值接口挖掘：
  - `similar-positions-new`（相似职位）返回空 list，需确认完整参数
  - `search/positions`（搜索 JSON API）返回 `isVerification:1` 需额外验证，SSR 已绕过
  - `associational-word`（联想词）、公司工商接口

> 私有仓库：逆向算法代码不公开。如需公开请手动切换 GitHub repo visibility。
