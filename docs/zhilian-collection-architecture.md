# 智联采集器最终架构方案：百万级岗位 + 公司维度聚合

> 日期：2026-08-10
> 目标：纯协议采集百万级「职位 + 公司」数据，支持按**公司维度**聚合全部在招岗位，Redis 分布式默认路线。
> 状态：已实现并可运行（Redis 分布式 + 双桶限速 + 公司补采闭环）。

## 1. 核心结论与验证范围

智联三个数据获取环节**全部纯协议、无浏览器、无挑战、无 cgate**：

| 环节 | 请求 | 响应形态 | 验证 |
|------|------|----------|------|
| 关键词搜索 | `GET sou.zhaopin.com/?kw=xxx&p=N` | SSR HTML 内嵌 `__INITIAL_STATE__.positionList` | ✅ 448/448 压测 |
| **公司名搜索** | `GET sou.zhaopin.com/?kw=公司名`（不带 jl = 全国） | 同上，返回**该公司全部在招岗位** | ✅ 紫光未来 32/32、希瑞 27/27 对上页面 count |
| 职位详情 | `GET fe-api.zhaopin.com/c/i/jobs/position-detailv2?number=xxx` | JSON | ✅ 匿名稳定，无 IP 信誉依赖 |

**关键突破（2026-08-08）**：公司维度在招岗位**不依赖 cgate 专用接口**（`searchPositionsCompany`，statusCode=200 但 data 空，逆向卡点）。改用**公司名当关键词走普通搜索接口**，返回即该公司全量在招岗位，且按城市参数可筛选（`jl=653` 只返回该城市）。不带 `jl` 参数 = 全国全量。

## 2. 总体架构

```
┌──────────────────────────────────────────────────────────────┐
│  阶段一: 关键词搜索采集 (现有能力)                             │
│  48 制造词 × 智联全城市 → sou 搜索 → 岗位号 + 公司号           │
│  → 搜索队列 → 详情队列 → positions + companies                │
└──────────────────────────┬───────────────────────────────────┘
                           ▼
┌──────────────────────────────────────────────────────────────┐
│  阶段二: 公司名搜索补采 (新)                                   │
│  SELECT company_number, company_name FROM companies          │
│  → 每公司: sou 搜索 kw=公司名(全国) → 该公司全部在招岗位号      │
│  → 详情队列 → positions 幂等 upsert (查缺补漏)                │
└──────────────────────────┬───────────────────────────────────┘
                           ▼
┌──────────────────────────────────────────────────────────────┐
│  阶段三: 公司聚合分析 (Python 查 PG)                          │
│  GROUP BY company_number → 每公司在招岗位全集 → 导出/筛选      │
└──────────────────────────────────────────────────────────────┘
```

## 3. 存储设计（PostgreSQL，现有四表 + 1 索引）

| 表 | 职责 | 现状 | 变更 |
|----|------|------|------|
| `positions` | 岗位主表（PK `position_number`，索引 `company_number`/`city_id`/`fetched_at`）| ✅ 已有 | 无 |
| `companies` | 公司去重表（PK `company_number`）| ✅ 已有 | **+索引 `company_name`**（阶段二按名搜索用）|
| `runs` | 运行统计 | ✅ 已有 | 无 |

**设计决策：不建 `company_positions` 映射表。**
`positions` 自带 `company_number`，公司聚合即一条 SQL：

```sql
-- 某公司全部在招岗位（已采集到）
SELECT * FROM positions WHERE company_number = 'CZ883210900';
-- 公司维度统计
SELECT company_number, company_name, COUNT(*) AS job_cnt
FROM positions GROUP BY company_number ORDER BY job_cnt DESC;
```

**幂等**：`position_number` PK + upsert → 阶段一/二重复采集天然去重，百万级不膨胀。

## 4. Redis 任务队列设计（双队列 + 类型前缀）

Redis 承担三件事：**调度**（搜索/详情双队列）+ **按出口身份限速**（搜索/详情独立桶）+ **共享风控/崩溃恢复**。

```
搜索队列  zhaopin:tasks:search   (LIST, 搜索桶 100/s, N worker)
  keyword:城市:关键词            ← 阶段一 关键词搜索任务 (消费时自动翻完所有页, producer 入队)
  company:CZ883210900            ← 阶段二 公司名搜索任务 (与关键词搜索合并)
        │  消费 → sou 搜索 → 提取岗位号 → rpush 详情队列
        │  兼 → 提取公司号 → rpush 搜索队列 (company: 任务, 查缺补漏)
        ▼
详情队列  zhaopin:tasks:position  (LIST, 详情桶 400/s, N worker)
  position:CC883210900Jxxx        ← 全部岗位详情任务 (去重)
        │  detailv2 → 有界批量事务 → positions + companies
        ▼
PostgreSQL
```

**改造点：搜索任务也进 Redis（不再 produce 端单进程串行）**。
- producer 只负责**生成任务**：城市×关键词入队（+ 公司号入队），不直接发搜索请求；关键词任务由 worker 自动翻页
- 搜索 worker 多进程消费搜索队列；每个 keyword/company 任务按页顺序请求，持续产出岗位号并写入详情队列
- 2026-08-10 校正：旧探针受 curl_cffi 默认 `max_clients=10` 和单岗位热点样本限制。详情 5 个实时岗位、池 500 的 10 秒档达到 573.8/s（2 次 211、0 其他异常）；池 600/800 触发 Windows `select()` 文件描述符限制。3 个岗位、池 40 持续 180 秒为 207.6/s，更适合作为稳态参考。当前生产目标调整为搜索 100/s、详情 400/s，提升后需持续压测。

**为什么搜索与详情分队列**：任务类型不同、消费逻辑不同（搜索任务消费后**产出新任务**，详情任务消费后**入库**）。两类任务分别配置并发和限速；同一出口内，相同类型的 worker 共享对应的 Redis 全局桶。

**为什么关键词与公司名搜索合并**：两者打同一接口、同一防线，任务格式一致（搜索一次），天然同队列。

**去重（两层）**：
1. **任务级**：任务 id 用 `SADD seen` 去重——`keyword:城市:关键词` 和 `company:公司号` 各自唯一，producer 幂等
2. **岗位号级**：搜索产出的 number 跨任务重复命中（同一岗位被多个关键词搜到），详情队列 `SADD seen` 去重——**已有机制，不需要新增**

**防崩溃**：
- Lua 原子执行去重+入队、完成、失败和回收状态转换
- `BLMOVE queue → processing:list` 原子领取；即使领取后未写时间戳就崩溃，staging list 仍可回收
- `processing` HASH 保存 Redis 服务器时间；搜索任务每页 heartbeat 续租
- 每次领取生成 claim token；旧 worker 的 token 无法完成、续租或重试已回收的新租约
- `attempts` HASH + EXPIRE 管理重试；`done_count` 计数器替代百万级 done SET
- `seen` SET 去重 → 跨 producer 幂等
- 类型前缀（`keyword:`/`company:`/`position:`）区分任务类型，同一套键结构照搬

**限速（分桶）**：`RedisRateLimiter` 使用 Redis 服务器时间的 Lua 时隙调度，以固定间隔分配请求。key 按 `ZHAOPIN_EGRESS_ID` 区分出口，再分 search/detail；不同出口不会错误共享同一总桶。每个桶同时维护 `issued` 和 `responded` 计数，分别表示已发出请求和已收到 HTTP 响应。

## 5. 阶段衔接

| 衔接 | 机制 |
|------|------|
| 阶段一 → 阶段二 | 方案 A（推荐）：阶段一 produce 时顺手把 `company_number` 入队为 `company:` 任务；或阶段一结束后 `SELECT company_number FROM companies` 批量入队 |
| 阶段二 内部 | `company:` 任务消费 → 搜公司名（翻页）→ 该公司岗位号入 `position:` 队列 → 同一批详情 worker 消费 |
| 阶段二 → 阶段三 | PG 数据完备后，Python 脚本 `GROUP BY company_number` 直接聚合 |

**同名公司边界**：公司名搜索可能命中同名公司（不同 `company_number`）。阶段二按 `company_number` **严格过滤**——只收目标公司的岗位，混合结果丢弃。

## 6. 速率与风控纪律

- **搜索/详情分桶**：当前目标搜索 100/s、详情 400/s；这是配置目标，不表述为服务端硬上限
- **详情批量持久化**：每个 worker 共享有界 `DetailWriteBatcher`，按 20 条或 20ms 聚合，以两个 PostgreSQL writer 事务写入。岗位先按岗位号排序，再按公司号去重排序更新，避免多 writer 在 `ON CONFLICT DO UPDATE` 时形成反向行锁；仅当批量事务成功后才确认 Redis 任务。
- **端到端实测**：搜索新关键词在 20 秒稳态窗口为 100.45 个有效响应/s。详情 25 万真实任务池的约两分钟样本为 45,632 个 HTTP 响应和 45,633 个持久化完成，约 352 条/s；400/s 保持为桶的上限配置。
- **连接池显式跟随并发**：不会再被 curl_cffi 默认 `max_clients=10` 静默截断
- **Redis 连接池显式跟随 worker 并发**：每个 worker 的连接池上限按 `search_concurrency + concurrency + 32` 计算（400/100 配置对应 532），避免 redis-py 默认上限导致 `MaxConnectionsError`
- **Windows 运行时边界**：curl_cffi 的 asyncio selector 使用 `select()`，本机池 600 以上会先触发文件描述符限制；这是客户端约束，不是服务端上限
- **共享风控**：异步生产路径使用 Redis 状态机，同一 `ZHAOPIN_EGRESS_ID` 的 worker 共享冷却窗口
- **211 有限重试**：有效岗位在高并发下可能偶发 211 后恢复 200，不能单次判定永久失效
- 遇到交互验证码后进入共享冷却并有限重试；IP 长期处于异常状态时由状态机延长冷却时间

## 7. run.py 一键配置（已实现）

交互式配置向导 + 自动运行：

```
run.py
1. 交互式逐项填写（[默认值]，回车用默认）:
   · 关键词:  逗号分隔 / 从 keywords.json 选择
   · 城市:    中文名或代码 / 从 cities.json 选择
   · 页数:    默认 5（仅单机调试模式使用；Redis keyword 任务自动翻页）
   · 模式:    Redis 分布式 (生产默认, 回车自动跑 produce → consume)
   · [Redis] worker数/并发/限速/是否清池/resume
   · DB 路径
2. 打印配置摘要 + 确认 (Y/n)
3. 确认 → 自动跑 (城市×关键词 笛卡尔积 → 公司名补采 → 聚合)
```

配置播种：
- `config/keywords.json` ← 51job-crawler 基础制造词（L1/L3/L4 去重，28 词）+ 手工补充焊锡膏客户词（SMT 岗位/代工模式，20 词）→ 当前 **48 词**（分析"哪些公司可能用焊锡膏"导向）
- `config/cities.json` ← 智联全城市（从 `search_base_data.json` 城市字典挖，code+name）
- 两电脑配置区分：保存记录 hostname/IP，加载时异机告警

## 8. 已完成的架构改造

1. **双队列调度**：`TaskQueue` 使用 `keyword:`/`company:`/`position:` 类型前缀，将搜索和详情任务拆分为独立 Redis 队列。
2. **可靠领取与恢复**：Lua 原子状态转换、`BLMOVE` staging list、Redis 服务器时间、heartbeat、claim token fencing，以及有限重试共同保证崩溃恢复和过期租约隔离。
3. **分桶限速与共享风控**：`RedisRateLimiter` 按 `ZHAOPIN_EGRESS_ID` 和请求类型划分 search/detail 桶；Redis 风控状态在同一出口的多进程之间共享。
4. **异步连接池**：curl_cffi `AsyncSession` 显式设置 `max_clients`，连接池不再被默认值 10 静默限制。
5. **阶段衔接与幂等入库**：搜索结果写入详情队列，岗位号使用 Redis `SADD` 去重，PostgreSQL 使用主键 upsert；公司补采严格按 `company_number` 过滤。
6. **统一入口与配置**：`run.py` 已提供 Redis 分布式向导。默认搜索并发为每 worker 100、全局限速为每出口 100 req/s；详情并发为每 worker 400、全局限速为每出口 400 req/s。
7. **压测与回归验证**：已覆盖搜索、详情并发阶梯、211 有限重试、队列恢复和默认配置回归；长期生产稳态仍需单独验证。

## 9. 风险与边界

- **完整性语义**：阶段二补采后，`positions` 是"搜索命中 + 公司名命中"的并集，**不是**公司全量在招岗位的绝对全集（公司名搜索本身的覆盖面受搜索接口索引影响）。作为分析筛选足够，但文档要明确语义
- **同名公司**：靠 `company_number` 严格过滤兜底
- **公司名变化**：公司改名后旧名搜索失效，增量重采时以 `companies.company_name` 最新值为准
- **搜索接口 IP 信誉**：公司名搜索与关键词搜索共享防线。当前目标为 100 req/s；长时间运行仍需监控 challenge、验证码、错误率和 p95，不能仅依据短时压测确定长期安全值。
