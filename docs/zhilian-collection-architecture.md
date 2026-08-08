# 智联采集器最终架构方案：百万级岗位 + 公司维度聚合

> 日期：2026-08-08
> 目标：纯协议采集百万级「职位 + 公司」数据，支持按**公司维度**聚合全部在招岗位，Redis 分布式默认路线。
> 状态：已实现并生产运行（Redis 分布式 + 双桶限速 + 公司补采闭环）。

## 1. 核心结论（全部实测验证）

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
│  112 制造词 × 智联全城市 → sou 搜索 → 岗位号 + 公司号          │
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
| `search_pool` | 命中记录（岗位×关键词/公司名）| ✅ 已有 | 无 |
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

Redis 只承担三件事：**调度**（搜索/详情任务都进 Redis，多进程分布式消费）+ **全局限速**（搜索+详情共享同一令牌桶）+ **防崩溃**（现有机制照搬）。

```
搜索队列  zhaopin:tasks:search   (LIST, 搜索 20/s + 详情 80/s 独立桶, N worker)
  keyword:城市:关键词            ← 阶段一 关键词搜索任务 (消费时自动翻完所有页, producer 入队)
  company:CZ883210900            ← 阶段二 公司名搜索任务 (与关键词搜索合并)
        │  消费 → sou 搜索 → 提取岗位号 → rpush 详情队列
        │  兼 → 提取公司号 → rpush 搜索队列 (company: 任务, 查缺补漏)
        ▼
详情队列  zhaopin:tasks:position  (LIST, 同速率, N worker)
  position:CC883210900Jxxx        ← 全部岗位详情任务 (去重)
        │  detailv2 → upsert positions + companies
        ▼
PostgreSQL
```

**改造点：搜索任务也进 Redis（不再 produce 端单进程串行）**。
- producer 只负责**生成任务**：城市×关键词×页码 笛卡尔积入队（+ 公司号入队），不直接发搜索请求
- 搜索 worker 多进程消费搜索队列，每任务 = 一次搜索页请求 → 产出岗位号入详情队列
- 压测证据（2026-08-08）：sou 搜索 max 33.5 req/s **0 触发防线**；详情 detailv2 峰值 111 req/s、**稳定 ~70/s（单 IP 服务器软限）**——据此拆独立双桶：搜索 20/s（风控敏感）+ 详情 80/s

**为什么搜索与详情分队列**：任务类型不同、消费逻辑不同（搜索任务消费后**产出新任务**，详情任务消费后**入库**）。分队列可独立调并发，但**限速共享**（见下）。

**为什么关键词与公司名搜索合并**：两者打同一接口、同一防线，任务格式一致（搜索一次），天然同队列。

**去重（两层）**：
1. **任务级**：任务 id 用 `SADD seen` 去重——`keyword:城市:关键词:页码` 和 `company:公司号` 各自唯一，producer 幂等
2. **岗位号级**：搜索产出的 number 跨任务重复命中（同一岗位被多个关键词搜到），详情队列 `SADD seen` 去重——**已有机制，不需要新增**

**防崩溃（复用现有）**：
- `processing` HASH(number→claimed_at) + `recover_stale()` 分布式锁回收 → worker 崩溃不丢任务
- `attempts` HASH + HINCRBY + EXPIRE → 失败重试 3 次后标记 failed，无内存泄漏
- `seen` SET 去重 → 跨 producer 幂等
- 类型前缀（`keyword:`/`company:`/`position:`）区分任务类型，同一套键结构照搬

**限速（分桶）**：`RedisRateLimiter` 滑动窗口 Lua，搜索/详情各自独立令牌桶（默认 搜索 20/s + 详情 80/s）。拆桶依据压测：搜索 max 33.5/s、详情 max 111/s（稳定 ~70/s，单 IP 服务器软限）均 0 防线升级；且详情 detailv2 无 IP 信誉依赖可高频、搜索风控敏感保持低频。并发由各队列 worker 数控制（`--search-concurrency`/`--concurrency` 独立）。

## 5. 阶段衔接

| 衔接 | 机制 |
|------|------|
| 阶段一 → 阶段二 | 方案 A（推荐）：阶段一 produce 时顺手把 `company_number` 入队为 `company:` 任务；或阶段一结束后 `SELECT company_number FROM companies` 批量入队 |
| 阶段二 内部 | `company:` 任务消费 → 搜公司名（翻页）→ 该公司岗位号入 `position:` 队列 → 同一批详情 worker 消费 |
| 阶段二 → 阶段三 | PG 数据完备后，Python 脚本 `GROUP BY company_number` 直接聚合 |

**同名公司边界**：公司名搜索可能命中同名公司（不同 `company_number`）。阶段二按 `company_number` **严格过滤**——只收目标公司的岗位，混合结果丢弃。

## 6. 速率与风控纪律

- **搜索/详情分桶**（实测 2026-08-08）：搜索 max 33.5 req/s、详情 max 111 req/s（**稳定 ~70/s，单 IP 服务器软限**）均 0 防线升级 → 独立双桶 搜索 20/s + 详情 80/s
- **并发独立控制**：搜索/详情协程数各自独立（`--search-concurrency` / `--concurrency`），够打满对应桶即可，不互相拖累（实测详情并发 10 已达单 IP ~70/s 上限，加协程无提升）
- 风控状态机（`utils/risk.py`）照常接入：防线分级/指数冷却/token 缓存，跨 run 持久化
- 遇交互验证码自动冷却重试，不硬闯（IP 长期被标记时，状态机会自动降速，无需预先保守）

## 7. run.py 一键配置（待实现）

交互式配置向导 + 自动运行：

```
run.py
1. 交互式逐项填写（[默认值]，回车用默认）:
   · 关键词:  逗号分隔 / 从 keywords.json 选择
   · 城市:    中文名或代码 / 从 cities.json 选择
   · 页数:    默认 5
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

## 8. 实施顺序

1. **Redis 队列改造**：TaskQueue 支持任务类型前缀（`keyword:`/`company:`/`position:`）+ 搜索/详情双队列 + 共享单令牌桶限速
2. **collect.py 改造**：produce 只生成任务（城市×关键词×页码入搜索队列，不再直接搜索）；消费端按前缀分发——搜索任务→sou 搜索→产出岗位号/公司号，详情任务→detailv2→入库
3. **阶段二公司名补采**：`company:` 任务消费 → 搜公司名 → 该公司岗位号入详情队列（严格过滤 company_number）
4. **阶段三聚合脚本**：`tools/company_aggregate.py`（查 PG → 公司岗位全集 → CSV/筛选）
5. **run.py + 配置播种**：交互向导 + keywords.json/cities.json
6. **压测验证**：搜索队列多进程消费（模拟万级任务）→ 岗位补采 → 聚合正确性

## 9. 风险与边界

- **完整性语义**：阶段二补采后，`positions` 是"搜索命中 + 公司名命中"的并集，**不是**公司全量在招岗位的绝对全集（公司名搜索本身的覆盖面受搜索接口索引影响）。作为分析筛选足够，但文档要明确语义
- **同名公司**：靠 `company_number` 严格过滤兜底
- **公司名变化**：公司改名后旧名搜索失效，增量重采时以 `companies.company_name` 最新值为准
- **搜索接口 IP 信誉**：公司名搜索与关键词搜索共享防线，批量阶段二采集需保持低频（预计几千公司 × 1-2s ≈ 1-2 小时/千公司）
