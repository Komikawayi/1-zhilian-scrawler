# 公司岗位列表获取方式调研（cgate 逆向 → 公司名搜索替代）

日期: 2026-08-08
任务: companydetail 公司全部在招岗位的协议获取方式

## 结论（已验证）

**公司全部在招岗位 = 公司名当关键词走普通搜索接口**，无需专用接口：

```
GET sou.zhaopin.com/?kw={公司名}&p=1..N   (不带 jl = 全国)
→ __INITIAL_STATE__.positionList = 该公司全部在招岗位 (严格过滤 companyNumber)
→ positionCount = 公司岗位总数, pages = 总页数
```

验证: 紫光未来科技(CZ883210900) 32/32 岗位、浙江希瑞(CZL1425835260) 27/27 岗位，
均与公司详情页 `onlinePositionsCount` 一致。岗位号可喂 position-detailv2 取详情。

## 为什么不用 cgate 专用接口

| 接口 | 结果 |
|------|------|
| `GET /c/i/company/search-position?number=` | 返回推荐岗位，number 参数无效（非公司过滤）|
| `POST cgate/positionbusiness/searchrecommend/searchPositionsCompany` | statusCode=200 但 data 空（即使浏览器真实会话 + 精确参数）|
| 公司名搜索 `sou.zhaopin.com/?kw=公司名` | ✅ 全量命中，纯协议无挑战 |

## 证据文件

- `initial_state_CZL1425835260.json` — 公司岗位页 SSR 完整 `__INITIAL_STATE__`（onlinePositions 结构）
- `initial_state_CZL1425835260_htm.json` — 公司详情页（.htm）SSR（在线职位 tab 结构）
- `ground_truth_sample.json` — cgate searchPositionsCompany 请求/响应样本（data 空 的复现证据）
- `dp_live_packets*.jsonl` — DrissionPage 抓包原始流（cgate 全部请求全景）
- `dp_live_hook.py` — XHR hook 脚本（定位前端真实调用点）
- `probe_cgate_search.py` — cgate 接口参数探测（S_SOU_* 参数族）
- `dp_ssr_and_paging.py` — SSR 分页结构探测（?p=N 翻页 + pages 字段）

## 落地

生产采集器已按此方案实现（`collect.py` company: 任务 → 公司名搜索 → 岗位入详情队列），
架构见 `docs/zhilian-collection-architecture.md`。
