# 1-zhilian-scrawler

智联招聘（zhaopin.com）搜索接口签名算法还原 + 纯协议采集器。

## 目标

还原智联招聘职位搜索接口的动态签名/鉴权参数，交付**脱离浏览器的纯协议 Python 采集器**（`web-protocol-recovery` 方法论）。

## 侦察工具链

| 工具 | 用途 |
|------|------|
| CloakBrowser (本地 0.5.3 + chromium-146) | CDP 原生网络/initiator、源码/断点、Hook、cookie/state 取证 |
| DrissionPage 4.1.1.2 | 真实 Chrome 接管、监听接口、登录态复用 |
| Python 3.14 + requests | 最终采集器 |

## 目录结构

```
1-zhilian-scrawler/
├── js_reverse_cache/   # 侦察素材（JS/WASM/HTML/抓包样本）
├── config/             # 配置
├── utils/              # header/cookie/signer/pagination 分模块
├── tests/              # 固定输入自检 / 验证脚本
├── main.py             # 采集器入口
└── README.md
```

## 状态

- [ ] 目标分类（startup gate）
- [ ] Phase 1: 确认真实请求路径
- [ ] Phase 2: 动态字段分类
- [ ] Phase 3: 定位突变点
- [ ] Phase 4: 离线重建签名
- [ ] Phase 5: 重复性验证

> 私有仓库：逆向工程签名算法代码不公开。如需公开请手动切换 GitHub repo visibility。
