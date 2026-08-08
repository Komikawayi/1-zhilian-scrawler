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
| ②' 详情数据 | **`position-detailv2` JSON API（纯协议无挑战）** | ✅ **默认路径**（匿名 448/448 压测零升级）|
| ③ 详情页兜底 | TCaptcha TDC（`cap_union_prehandle`/`tdc.js`/`new_verify`）| 🔬 **链路+边界已摸清并封存**：协议链路/POW/真实请求体已还原（见 `tasks/zhilian-detail-tdc-002/`），但 verify 需真实浏览器 collect，且 ticket 绑定浏览器指纹，纯协议无法解锁 |
| ④ fe-api 动态参数 | `_v` / `x-zp-page-request-id` / `x-zp-client-id` | ✅ 非签名（随机/无参/真实参都 200）|

### 关键结论（实测）

- **搜索** `sou.zhaopin.com`：EdgeOne 按 TLS/HTTP2 指纹放行，`curl_cffi chrome` 直接过，302 后取 SSR
  `__INITIAL_STATE__.positionList`（20 条/页）。kw 编码在服务端完成，客户端无需自实现。
  多关键词/多城市/翻页 positionCount 稳定。
- **职位详情（默认路线一）** `position-detailv2` JSON API：`number` 用搜索页 `positionList.number`，
  纯协议直接拿 `{ detailedPosition(67字段), detailedCompany(22字段), taskId }`，**448/448 压测零升级、
  0 挑战、0 验证码、无 IP 信誉依赖**，`jobDesc` 完整。比 SSR 快（5KB vs 1.7MB）、无 Node 依赖。
  匿名搜索池实测远超 5 页（positionCount 恒显示 100 但 p1-p8 全唯一）。
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
一键采集:      py run.py              (交互向导 → Redis 分布式自动跑)
手动分布式:    py collect.py --produce --kw <词> --cities <码>
               py collect.py --consume --workers N --concurrency N --search-concurrency 10 --search-rate 20 --detail-rate 80
公司补采:      py collect.py --produce --companies <公司号,...>
聚合分析:      py tools/company_aggregate.py --company <公司号>
SSR 兜底:      py main.py --detail-urls 自动过 JS Challenge (LEGACY)
```

## 架构

```text
run.py (一键入口: 交互配置向导 -> 自动调用 collect.py)
  -> collect.py (Redis 分布式: 搜索/详情双队列, 多进程 worker)
       search 队列 (zhaopin:tasks:search)
         keyword:城市:关键词       [阶段一] 关键词搜索任务 (自动翻完所有页, produce 生成)
         company:公司号             [阶段二] 公司名搜索补采任务
              | worker 消费 -> sou.zhaopin.com 搜索
              |  -> 岗位号入详情队列 + 公司号/名入 companies 表 + company: 任务
              v
       position 队列 (zhaopin:tasks:position)
         position:岗位号            [详情] position-detailv2 JSON API
              | worker 消费 -> fe-api/c/i/jobs/position-detailv2?number=
              v
       PostgreSQL (positions / companies / search_pool / runs)

tools/company_aggregate.py  公司岗位聚合分析 (GROUP BY company_number)
tools/seed_config.py        配置播种 (keywords.json 制造词 + cities.json 370 城市)
main.py                     [LEGACY] 顺序单并发 CLI (保留 SSR 挑战兜底 --detail-urls)
```

关键模块：

```text
run.py                      [生产主入口] 交互式配置向导 + 自动运行 (Redis 分布式)
collect.py                  [采集 CLI] produce/consume/stats/single 四模式
main.py                     [LEGACY] 顺序单并发 CLI (保留 SSR 挑战兜底 --detail-urls)
tools/company_aggregate.py  公司岗位聚合分析 (按 company_number 聚合全部在招岗位)
tools/seed_config.py        配置播种 (51job 关键词 + 智联全城市 -> config/*.json)
tools/login_collect.py      登录态采集 (简历/消息/投递/VIP/简历诊断)
utils/task_queue.py         Redis 双队列 (search: keyword/company + position, 崩溃安全)
utils/async_client.py       异步客户端 (curl_cffi AsyncSession, curl_infos 网络分段) + 共享令牌桶/Redis 全局限速
utils/latency_stats.py      分环节耗时统计 (实时均值 + 结束总结表, TTFB/总耗时/本地埋点)
utils/storage_pg.py         PostgreSQL 存储层 (asyncpg 连接池, 百万级, 主存储)
utils/pipeline.py           单机流水线 (调试用, 生产走 Redis 分布式)
utils/risk.py               风控状态机 (防线分级/自适应速率/指数冷却/token 缓存, 跨 run 持久化)
utils/http_client.py        curl_cffi chrome 指纹客户端 (同步, legacy 路径)
utils/fe_api.py             匿名接口 (position-detailv2) + 登录态接口 + 会话过期检测
utils/session.py            at/rt 登录会话加载/保存 (config/zhilian-session.local.json)
utils/challenge.py          fetch_job_detail: SSR + JS Challenge 求解 (兜底)
utils/parser.py             SSR 解析 (搜索 positionList + 详情 v2/SSR)
utils/output.py             CSV 输出 (UTF-8 BOM)
tools/eo_solve.js           Node vm 挑战执行器 (SSR 兜底路径用)
```

> 📄 风控机制分析（登录态/行为信号/心跳/请求节奏/数美）见 **[docs/zhilian-risk-control-analysis.md](docs/zhilian-risk-control-analysis.md)**。
> 📄 采集架构方案（百万级 + 公司维度聚合 + Redis 双队列设计）见 **[docs/zhilian-collection-architecture.md](docs/zhilian-collection-architecture.md)**。

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

### 一键运行（推荐）

```powershell
py run.py
```
交互式向导：填关键词/城市（或从种子列表选）→ 确认 → 自动跑 Redis 分布式采集
（produce 生成任务 → consume 多进程消费 → PostgreSQL 入库）。

### 手动分布式（collect.py）

```powershell
# 1. 生成任务池 (城市×关键词 笛卡尔积; keyword 任务消费时自动翻完所有页)
py collect.py --produce --kw smt,pcba,贴片 --cities 653,530 --clear

# 2. 多进程消费 (搜索 + 详情双队列, 各自独立限速桶: 搜索 20/s + 详情 80/s)
py collect.py --consume --workers 4 --concurrency 10 --search-concurrency 10 --search-rate 20 --detail-rate 80

# 3. 队列进度
py collect.py --stats
```

**自动查缺补漏**：搜索 worker 消费 keyword 任务时，把命中的公司号生成 company: 任务；
company 任务消费时按**公司名搜索**（全国）拿该公司**全部在招岗位**（严格过滤
company_number，SSR pages 精确分页），岗位入详情队列 → 每个公司聚合完整。

### 公司名补采（对已知公司号）

```powershell
py collect.py --produce --companies CZ883210900,CZL1425835260
py collect.py --consume --workers 2 --concurrency 8
```

### 公司聚合分析

```powershell
py tools/company_aggregate.py --top 20                 # 岗位数 TOP20 公司
py tools/company_aggregate.py --company CZ883210900    # 某公司全部在招岗位
py tools/company_aggregate.py --city 653               # 某城市岗位
py tools/company_aggregate.py --kw smt --export out.csv  # 关键词筛选导出
```

### 配置播种

```powershell
py tools/seed_config.py   # 生成 config/keywords.json (基础制造词) + cities.json (370 城市)
```
数据来源：51job-crawler 基础词（SMT/PCBA/贴片/回流焊/AOI/SPI/锡膏…）+ 手工补充焊锡膏客户词（SMT岗位/代工模式）至 **48 词**，当前 `keywords.json` 即全量。

### 单机调试（非分布式）

```powershell
py collect.py --single --kw smt --cities 653 --pages 2 --concurrency 10
```

### 兜底路径（main.py，[LEGACY] 保留 SSR 挑战）

```powershell
py main.py --detail --detail-urls "https://www.zhaopin.com/jobdetail/CCL1480117890J40614881205.htm" --output output/zhaopin_detail.csv
```
触发交互验证码时风控状态机自动指数冷却重试（`config/zhilian-risk.local.json` 跨 run 记忆）。

### 规模化验证

- **分桶限速压测（2026-08-08）**：搜索 max 33.5 req/s、详情 111 req/s 峰值均 0 防线升级 → 拆独立双桶：搜索 20/s（风控敏感）+ 详情 80/s
- **详情单 IP 软限（2026-08-08 复测）**：detailv2 稳定吞吐 **~70/s**（并发 10/20/30/40、纯请求/含写库实测一致）——智联对高频 detailv2 静默限速，80/s 桶实际 ~70/s；**搜索未受限**（max 32/s）
- **分布式链路（2026-08-08）**：2 关键词页 → 31 家公司补采 → **1382 岗位 0 失败**；4 worker 压测 2282 岗位全 done；紫光未来 32/32 精确补采
- **历史基准**：448 条顺序压测 100% 成功（0.54/s）；Phase B Redis 1705 条 100%（~114s）

> 登录态说明：采集**不依赖登录态**（匿名可用）。搜索 `positionCount` 恒显示 100 但实际可翻页远超 5 页（实测 p1-p8 全唯一，p=50 仍有数据）。

**Redis 隔离部署**：
```bash
docker network create zhilian-net
docker run -d --name zhilian-redis --network zhilian-net -p 127.0.0.1:6379:6379 \
  -v zhilian-redis-data:/data redis:7-alpine redis-server --appendonly yes
docker update --restart unless-stopped zhilian-redis
```
独立桥接网络 `zhilian-net`、仅绑 `127.0.0.1`（不暴露局域网）、独立数据卷、`appendonly` 持久化——与同机其他容器/服务完全隔离。

**PostgreSQL 隔离部署**（百万级存储）：
```bash
docker run -d --name zhilian-postgres --network zhilian-net -p 127.0.0.1:5433:5432 \
  -e POSTGRES_USER=zhilian -e POSTGRES_PASSWORD=<pwd> -e POSTGRES_DB=zhilian \
  -v zhilian-postgres-data:/var/lib/postgresql/data postgres:16-alpine
docker update --restart unless-stopped zhilian-postgres
```
连接串存 `config/db.local.json`（gitignored，`{"db_url": "postgresql://zhilian:...@127.0.0.1:5433/zhilian"}`），或环境变量 `ZHAOPIN_DB_URL`。asyncpg 连接池、JSONB、原生并发写（多 worker 进程）。

### 测试

```powershell
py -m pytest tests/ -v                          # 固定输入自检 (challenge 求解 + v2/SSR 详情解析 + 队列/存储)
ZHAOPIN_NETWORK_TEST=1 py -m pytest tests/ -v   # 含真实网络测试
```

### collect.py 常用选项

```text
--produce                生成任务 (城市×关键词 → 搜索队列; 消费自动翻页)
--consume                多进程消费 (搜索+详情双队列)
--single                 单机流水线 (调试用)
--stats                  队列进度
--kw <词>                关键词 (逗号分隔多个)
--cities <码>            多城市代码 (逗号分隔; 默认全国)
--companies <号>         公司号列表 (生成 company: 补采任务)
--pages <n>              仅 --single 单机模式生效 (每关键词页数; produce 自动翻页无上限)
--workers <n>            消费 worker 进程数
--concurrency <n>        详情并发协程数/worker (默认 10, 够单IP~70/s)
--search-concurrency <n> 搜索并发协程数/worker (默认 10, 打满 20/s 桶)
--rate <n>               [兼容] 全局旧限速 (搜索+详情同值; 优先用分桶参数)
--search-rate <n>        搜索桶限速 req/s (默认 20, 风控敏感)
--detail-rate <n>        详情桶限速 req/s (默认 80; 服务器单IP软限~70/s)
--clear                  produce 前清空对应队列
--db <url>               PostgreSQL URL
--redis <url>            Redis URL
```

## 运行文件

```text
config/keywords.json        关键词宇宙 (48 制造词: 51job 基础 28 + 焊锡膏客户词 20, 可编辑补充)
config/cities.json          智联城市列表 (370 城市, code+name)
config/*.local.json         本地敏感配置 (gitignored: db/session/risk/run)
output/                     导出 CSV / 测试产物
js_reverse_cache/           逆向素材 (分类: assets/analysis/data/tools/tasks)
js_reverse_cache/assets/    原始证据素材 (js/html/network 抓包)
js_reverse_cache/analysis/  分析脚本 (probe 探测 / tdc 逆向)
js_reverse_cache/data/      数据字典 (search_base_data.json)
js_reverse_cache/tasks/     任务证据目录 (tdc-001/002, risk-phase0, company-positions)
docs/                       逆向分析文档 + 架构方案
```

## 日志与进度

采集终端默认**只显示**错误/警告 + 4 行实时进度（每秒 ANSI 刷新）：

```
[搜索] 排队   12 处理中   3 完成 48210 失败  1 | 20.0/s    [详情] 排队   8 处理中  2 完成 38124 失败 0 | 80.0/s
[消费] 搜索:SMT工程师|杭州                    | 详情:工艺工程师|杭州
[运行] 01:23:45   总完成 86334 总失败 1   综合 96.7/s   风控:ok
[延迟] 搜索 449ms(TTFB 446) | 详情 211ms(TTFB 197) | 解析 1.2ms | 入库 1.8ms
```

- 行1：搜索/详情双桶排队/处理中/完成/失败 + 各自实时速率
- 行2：最近消费明细（搜索:关键词/公司 | 详情:岗位名|城市）
- 行3：任务运行时间 + 总完成/失败 + 综合速率 + 风控状态
- 行4：分环节延迟（**累计均值**，每秒刷新；worker0 显示本进程统计，各 worker 逻辑相同故代表整体）

**任务结束分环节耗时统计**：每个 worker 收敛后打印对齐统计表（终端 + 日志同步）：

```
=== worker 1 分环节耗时统计 ===
  环节      样本  均值    p50     p95     max       (TTFB)      
  搜索网络     128   449ms   432ms   511ms   585ms  (446ms)     
  详情网络     812   211ms   208ms   262ms   300ms  (197ms)     
  搜索解析     128     4ms     4ms     5ms     6ms  -           
  详情解析     812     1ms     1ms     1ms     2ms  -           
  详情入库     812     2ms     2ms     2ms     3ms  -           
  搜索入库    2560     1ms     1ms     2ms     2ms  -           
  冷却等待       2  3000ms  3000ms  3000ms  3000ms  -           
```

主进程最后输出 `采集完成: 总耗时 00:12:34, 日志见 output/logs/`。

**采集指标设计理念**（完整分析见 `docs/zhilian-crawler-pipeline.md` §3/§4）：
- **只采网络 TTFB + 总耗时**：TTFB = 服务端处理 + 1 RTT，是单请求延迟核心、直接决定并发需求（实测搜索 ~450ms / 详情 ~200ms）；总耗时 = 端到端，对应吞吐
- **不采 DNS/TCP/TLS**：keep-alive 稳态下每连接只发生一次 ≈ 0，是链路/系统属性非采集可控，采了只会污染统计误导分析（离线深度分析用 `js_reverse_cache/analysis/probe/net_latency_probe.py`）
- **本地埋点**（解析 / DB 写 / 冷却等待）：每次请求都发生，用于证明木桶在服务端还是本地（实测解析 1-4ms、入库 1-9ms，均远小于网络 → 木桶在服务端）
- **生产 v2 路径无本地解密/签名**：detailv2 纯 JSON 无挑战、参数非签名，本地零计算，无需采集
- 统计口径：均值看典型、p50 看中位、p95 看尾部最坏常态、max 抓异常——四者配合才能定位木桶，单看均值会被极端值骗

**日志分级**：控制台 StreamHandler 只显示 WARNING+（UI 交给进度显示器）；**完整 INFO 日志落盘** `output/logs/collect-<时间戳>-<pid>.log`（每进程一份，避免多进程写同一文件竞争，gitignored）。排查历史问题看日志文件，终端看进度/错误。

## 时间字段语义 (positions 表)

| 字段 | 语义 | 更新时机 |
| --- | --- | --- |
| `publish_time` | **岗位最新更新时间**（智联详情页"更新时间"）| 雇主刷新职位时变，重采时写入最新值 |
| `first_seen_at` | **首次入库时间**（第一次采集到该岗位）| 仅首次 INSERT 写入，重采不覆盖；存量 NULL 重采时补近似值 |
| `fetched_at` | **最近采集时间**（我们最后一次采到该岗位）| 每次重采刷新 |

> **智联没有独立的"更新时间"字段** —— 详情页展示的"更新时间"即 `positionPublishTime`（浏览器实测确认）。
> 分析用法：`publish_time > first_seen_at` 的岗位 = 入库后被雇主刷新过 → 活跃岗位筛选；
> 三字段对比可还原"首发 → 刷新 → 重采"完整时间线。

## 限速与风控纪律

- 搜索页 + `position-detailv2`（推荐详情路径）纯协议稳定，**不受详情页 IP 信誉影响**（20/20 实测）。
- SSR 兜底路径：详情页 challenge 自动本地求解，token 可复用 1 小时；**IP 信誉是共享资源**，
  大量探测会升级到交互验证码（验证码放行绑定浏览器指纹，curl_cffi 无法复用），需冷却 5-30 分钟。
- curl_cffi 0.16 在 HTTP/2 下 Set-Cookie 解析异常（`r.headers['set-cookie']` 只返回 `path=/`）；
  SSR 重放只需带 `EO-Bot-Js-Token`（首次重放 1.7MB 已验证），`acw_tc`/`cdn_sec_tc` 非必需。
- 逐条采集建议 1-2 秒/请求（`--sleep 1.5` 默认），高并发易触发风控。

## 风控状态机（utils/risk.py）

采集全程接入跨 run 持久化的 IP 信誉状态机（`config/zhilian-risk.local.json`，gitignored）：

- **防线分级**：`ok(直通) → challenge(JS挑战,可解) → captcha(交互验证码,需冷却) → cooling`
- **自适应速率**：challenge 期间请求间隔 ×2，冷却期 ×4（配合 `http_client` 限速）
- **指数冷却**：验证码触发后 60s/120s/240s… 递增（上限 30min），跨 run 记忆，下次启动自动等完冷却
- **EO-Bot-Js-Token 缓存**：挑战 token 1h 复用，不再每请求重复执行 Node
- **搜索路径风控重试**：单页触发验证码自动冷却后重试（3 轮），不再硬中断整批

## 失败策略

| 失败 | 结果 |
| --- | --- |
| 超时 / 连接重置 | 指数退避 + 重试（http_client 3 次）|
| position-detailv2 业务错误（code≠200）| 报错（含 message）|
| EdgeOne JS Challenge（SSR 详情首跳）| 优先复用缓存 token → 否则 Node vm 求解 → 重放 |
| 重放后仍 Challenge | 重试（默认 2 次）+ 状态机连续计数升级 |
| 交互验证码 `Security Verification` | 进入指数冷却（跨 run 记忆），冷却后自动重试 |
| 搜索页中途验证码 | 自动冷却重试（3 轮）|
| `__INITIAL_STATE__` 缺失 / JSON 解析失败 | 报错 |

## 逆向研究命令

侦察与验证工具在 `js_reverse_cache/`（均为一次性研究脚本，不入采集主流程，按主题归拢）：

```text
js_reverse_cache/analysis/probe/probe_feapi.py         fe-api 纯协议对照 (随机/无参/真实参)
js_reverse_cache/analysis/probe/e2e_detail.py          详情页 challenge 端到端验证
js_reverse_cache/analysis/probe/probe_trigger.py       防线触发条件探测 (高频访问观察升级)
js_reverse_cache/analysis/probe/stress_test.py         单 IP 顺序压测 (历史, 已升级为 collect.py Redis 测试)
js_reverse_cache/analysis/probe/test_replay_cookie.py  requests vs curl_cffi 重放对照
js_reverse_cache/analysis/tdc/                         TDC 逆向 (iv8_*.py / extract_webpack_module / tdc_iv8_e2e)
```

详细逆向过程、防线分级、TDC 协议链与踩坑记录：
- `docs/zhilian-edgeone-reverse-analysis.md`
- `js_reverse_cache/tasks/zhilian-detail-tdc-001/report.md`

## 遗留工作

- [x] 详情采集端到端（position-detailv2 纯协议 20/20，推荐路径）
- [x] TCaptcha TDC 协议链还原（prehandle/tdc.js/collect/eks/ft/POW；`js_reverse_cache/tasks/zhilian-detail-tdc-002/`）
- [x] **TDC 边界确认并封存**：纯协议 verify 需真实浏览器 collect（Node 沙箱环境指纹产不出 6704B 级遥测）；即使拿到 ticket 也绑定浏览器指纹，curl_cffi 无法解锁业务页 → 不作为采集器交付路径
- [ ] SSR 兜底路径端到端复验（需 IP 信誉恢复；首次已证明可行）
- [ ] 更多有价值接口挖掘：
  - `similar-positions-new`（相似职位）返回空 list，需确认完整参数
  - `search/positions`（搜索 JSON API）返回 `isVerification:1` 需额外验证，SSR 已绕过
  - `associational-word`（联想词）、公司工商接口

> 私有仓库：逆向算法代码不公开。如需公开请手动切换 GitHub repo visibility。
