# 智联招聘风控机制分析

> 目标：摸清智联招聘服务端爬虫校验与反爬策略（心跳机制、用户行为打分、惩罚链），提高单个 IP 爬取稳定性。
> 日期：2026-08-06
> 关联：`docs/zhilian-edgeone-reverse-analysis.md`（EdgeOne 挑战还原）、`js_reverse_cache/tasks/zhilian-risk-phase0/`（摸底素材）

## 1. 智联风控体系全景

智联是**多层叠加**的风控体系，各层职责独立：

| 层 | 防线 | 作用 | 状态 |
|----|------|------|------|
| ① 访问门控 | EdgeOne | TLS/HTTP2 指纹 (JA3/JA4) + IP 信誉 | ✅ 已还原（curl_cffi chrome）|
| ② 详情页 | EdgeOne JS Challenge | 91-opcode VM → EO-Bot-Js-Token | ✅ 已还原（Node vm）|
| ③ 交互验证 | TCaptcha | IP 信誉差触发，绑定浏览器指纹 | 🔬 链路已摸清，未落地 |
| ④ 设备指纹 | **数美 Shumei** | 指纹采集 + 画像上报 → 行为打分 | 🔬 接口已抓；deviceSn 纯协议可生成（研究已验证，实现未落地），画像加密链路未还原 |
| ⑤ 反作弊 | **百度秒针** | miao.baidu.com/abdr | ❓ 低价值 |
| ⑥ 行为埋点 | **神策** | sa.gif 行为事件流 | ✅ 已盘点（非风控核心）|
| ⑦ 登录验证 | 阿里云 NoCaptcha | 登录时滑块 | ✅ 已定位端点 |

**核心结论**：智联的「用户行为打分」主要载体 = **数美**（设备指纹 + 画像上报）。EdgeOne 管流量入口，数美管设备/行为风险。

## 2. 登录态机制

- **登录方式**：密码 `POST /v4/account/login`、短信 `/v4/sms/login`、微信/APP扫码/第三方；验证=阿里云 NoCaptcha
- **登录态传递**：**URL 查询参数 `at`（access token）+ `rt`（refresh token）**，非 header/cookie
  ```
  所有 fe-api 请求带 ?at=<32hex>&rt=<32hex>
  at/rt 从 cookie 读取, 前端 JS 附加到 URL
  ```
- **纯协议重放**：curl_cffi 带 at/rt 直接访问登录态接口（无需浏览器），已验证简历/消息/投递/VIP 全通
- **会话文件**：`config/zhilian-session.local.json`（gitignored），采集器自动加载 + 会话过期检测（code=210）

## 3. 行为信号（Phase 2a）

| 上报 | 端点 | 内容 | 触发 |
|------|------|------|------|
| 数美设备上报 | `cgate/userpassport/report/reportShuMeiDevice` | boxId(89字符B开头)/boxData | 页面加载 + 每小时心跳 |
| 数美设备画像 | `fp-it.portal101.cn/deviceprofile/v4` | RSA 加密 ep + data（完整指纹）| 页面加载 |
| 神策埋点 | `ds.zhaopin.cn/sa.gif` | 行为事件（passport_vist/ui_performance）| 页面事件 |
| 百度秒针 | `miao.baidu.com/abdr` | 反作弊 | 页面加载 |

数美 deviceprofile 请求体：`{appId, organization, ep(RSA短), data(RSA长), os:"web", encode:5, compress:2}`

## 4. 心跳机制

- **数美心跳**：智联 device-fingerprint-sdk 每 **1 小时**检查 deviceSn 有效期（23h 内有效），过期重新上报
- deviceSn 写 cookie `x-zp-device-sn`（24h）+ localStorage `device-sn-update-time`
- 采集器**不需要**模拟数美心跳（它是浏览器侧行为上报；纯协议采集用 at/rt 即可）

## 5. 请求节奏结论（Phase 2c 实测）

- curl_cffi Chrome 指纹信誉稳定：详情页 0.3s 高频也只触发 JS Challenge（可本地解），**不升级验证码**
- 登录态 fe-api 高频稳定
- 防线升级到交互验证码需要「大量失败探测」或「异常指纹」（如 requests 无指纹）
- **生产建议**：1-2s/请求稳定；无需过度降频

## 6. 采集器落地

```
tools/login_collect.py        登录态采集 (简历/消息/投递/VIP/简历诊断)
utils/session.py              at/rt 会话加载/保存/认证参数
utils/fe_api.py               登录态接口 + 会话过期检测 (LoginExpiredError)
config/zhilian-session.local.json  登录凭据 (gitignored)
```

用法：
```bash
py tools/login_collect.py                          # 采集登录态数据
py main.py --detail --detail-numbers "CCLxxx"      # [LEGACY] 匿名职位详情 (无需登录; 生产走 collect.py)
```

## 7. 数美 SDK 深度还原结论（Phase 2b，已完成）

对 `fp.min.js`（370KB 混淆）完整还原：

**指纹采集**：70+ 字段（canvas `pt`/字体 `nw`/`rj`/WebGL `ob`/`pz`/UA/屏幕/行为检测 `de`），DES 掩码（固定 8 字符 key）

**加密链**（已验证可逆）：
```
uid(UUIDv4) --RSA-1024--> ep (base64, 172字符)
uid --md5[0:16]--> priId --AES-128-CBC(key=priId, IV=0102030405060708, ZeroPadding)--> data(hex)
innerJSON 加 kz 校验和: kz = btoa(DES('z2qd9f1w', md5(canonical(inner))))
  ⚠️ canonical 陷阱: undefined 字段(如 lk)被 kz 计入但被 JSON.stringify 丢弃, 本地复现须手工补
boxData = 'D' + btoa(JSON({appId, organization, ep, data, os:"web", encode:5, compress:2}))
SMID = deviceprofile/v4 响应 deviceId (88字符, 存 .thumbcache_ cookie, ≈719 天有效)
boxId = 'B' + SMID (89字符)
```

**本地合成可行性：完全可行，且智联不校验绑定（关键）**
- DES/AES/RSA/canonical/kz 全部可本地复现（Node vm 可完整跑 SDK）
- 需要真实环境的字段（canvas/字体/WebGL）固定环境稳定，**捕获一次缓存可长期复用**
- **deviceSn 纯协议生成已验证**：`POST reportShuMeiDevice {boxId: 任意非空}` → code=200 + 新 deviceSn（智联不校验 boxId 指纹绑定）；空 boxData 不生成
- **落地状态**：采集接口均不需 deviceSn（`x-zp-device-sn` 是 cookie 非 header，已确认不必要）；`utils/device.py` 已删除，研究验证结论保留于此

## 7.1 遗留

- [x] 数美 SDK 算法深度还原 + deviceSn 纯协议生成 —— Phase 2b 完成
- [ ] at/rt 会话有效期/续期机制实测（rt 如何续期）
- [ ] TCaptcha iv8 落地（详见 EdgeOne 文档）
- [ ] 规模化多账户采集（当前 1 账户）
- [ ] EO Token 一次性/URL 绑定验证（受 IP 信誉限制，需 IP 好时复测）

## 8. 隐藏机制评估（逐层验证）

对「非主动上报的被动风控」逐层评估，标注智联实测结论：

### 8.1 流量层

| 机制 | 智联实测 | 证据 |
|------|----------|------|
| EO-Bot-Js-Token 时效 | ✅ 已证实（cookie max-age=3600，过时重挑战）| 首跳 challenge → 解 token → 重放 |
| EO-Token 一次性/URL 绑定 | ⚠️ 待验证 | 重放总被验证码拦 = **IP 信誉问题非 token 消耗**（有效 token 通过校验才升级验证码，垃圾 token 直接 challenge）|
| Sec-Fetch-* 缺失检测 | ❌ 未发现 | **智联 fe-api 请求本身不带 Sec-Fetch-***（浏览器采样），非关键 |
| TCP/HTTP2 流量指纹 | ⚠️ 存在但难验证 | curl_cffi 模拟有限；云函数/机房特征可能被嗅探 |

### 8.2 账号行为层

| 机制 | 智联实测 | 证据 |
|------|----------|------|
| 账号-设备-IP 关联 | ✅ **unread-message 不校验 deviceSn**（无/真实/垃圾 deviceSn 全 200）| 对照实验 3 变体 |
| deviceSn 即时拦截 | ❌ 未发现（至少 unread-message）| 同上；高价值接口/累积风控分待查 |
| 行为序列检测 | ⚠️ 存在风险 | 采集器 Referer 固定/请求模式机械，可能累积嫌疑分 |
| 蜜罐链接 | ❓ 未检查 | 需扫页面 display:none/诱饵 URL |

### 8.3 数美隐形反杀

| 机制 | 智联实测 | 证据 |
|------|----------|------|
| 环境一致性校验 | ⚠️ 存在（本地合成难）| deviceprofile 需 canvas/WebGL 指纹（RSA 加密，本地合成成本高）|
| deviceSn 脏数据污染 | ⚠️ 存在风险 | 同一 deviceSn 纯协议高频可能被标 Bot |
| 被动画像比对 | ⚠️ 外部查询 | 智联可向数美查 IP/UA 历史风险 |

### 8.4 数据层

| 机制 | 智联实测 | 证据 |
|------|----------|------|
| 无感验证码（captchaKey 字段）| ❌ **未发现** | search_base_data / position-detailv2 响应干净（risk/bot 命中是职位类型「合规风控」「机器人算法」误报）|
| 假数据/状态码欺骗 | ⚠️ 待验证 | 需多次请求对比响应一致性 |
| HTTP 200 但内容污染 | ⚠️ 待验证 | 需对比「已下线」类响应是否伪装 |

### 8.5 百度秒针

- ⚠️ 跨站广告反作弊关联：可能同步设备指纹/IP 风险标签给智联，垫高基础风险分。低触发概率。

## 9. 采集器加固建议（基于隐藏机制评估）

1. **请求头补充**：curl_cffi 默认空头，需手动补 `sec-ch-ua`/`sec-ch-ua-platform`（浏览器带，防工具嫌疑）
2. **Referer 链模拟**：登录态接口带 `referer: https://i.zhaopin.com/`（已做）；搜索→详情→投递的 Referer 链要模拟
3. **deviceSn 关联**：unread-message 不校验，但**高价值接口（简历/投递）可能校验**——纯协议如遇异常，需补 deviceSn（用浏览器捕获的，或数美合成）
4. **行为节奏**：登录态接口 1-2s/请求；避免同一接口机械高频
5. **EO Token 消耗**：重放受 IP 信誉限制，IP 被标记时重放必升级验证码——采集器遇验证码即停，勿硬闯（已实现 `--captcha-cooldown`）
6. **数据一致性校验**：采集器可加「同 number 多次响应 diff」检测假数据/污染

## 10. 风控状态机落地 + 规模化压测结论（2026-08-07）

### 10.1 采集器风控状态机（utils/risk.py）

把 §9 的加固建议固化为代码：跨 run 持久化的 IP 信誉状态机（`config/zhilian-risk.local.json`，gitignored）。

- 防线分级 `ok(直通) → challenge(JS挑战) → captcha(验证码) → cooling`
- 自适应速率：challenge ×2 / 冷却 ×4（配合 http_client 限速）
- 指数冷却 60s→120s→240s…（上限 30min），跨 run 记忆，下次启动自动等完
- **EO-Bot-Js-Token 1h 缓存复用**（§9.5"勿硬闯"的代码化：不再每请求跑 Node 挑战）
- 搜索路径遇验证码自动冷却重试（3 轮），不再硬中断整批
- 对应 §9 建议：3(deviceSn 预留在 device.py)、4(1-2s/请求已配置化)、5(遇验证码即停→状态机冷却)

### 10.2 单 IP 规模化压测（路线一：搜索 SSR → position-detailv2）

`js_reverse_cache/stress_test.py`，匿名，5 关键词 × 5 页搜索池 → 448 条详情连续采集：

| 指标 | 结果 |
|---|---|
| 成功率 | **448/448 = 100%** |
| 风控升级 | 0（全程 `ok`，0 挑战 0 验证码）|
| 速率因子 | 全程 1.0 |
| 总耗时 | ~14.6 分钟（0.54 条/s）|
| 数据完整 | 36 字段，0 空 job_desc |

**结论**：路线一（position-detailv2）在当前速率下单 IP 稳定成立，且不依赖登录态。
搜索 `positionCount` 恒显示 100 但实际可翻页远超 5 页（实测 p1-p8 全唯一、p=50 仍有数据）。

### 10.3 边界提醒

- 路线二（SSR 挑战）规模化仍烧 IP 信誉，仅作兜底低频使用。
- 高价值登录态接口（简历/投递）若异常需补 deviceSn（device.py 已就绪）。
- 本压测为单 IP 单会话；跨会话/多 IP 行为未验证。
