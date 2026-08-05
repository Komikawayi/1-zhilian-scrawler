# 1-zhilian-scrawler

智联招聘（zhaopin.com）搜索接口签名算法还原 + **纯协议 Python 采集器**（已跑通）。

## 逆向分析结论

### 真实请求路径 (Phase 1)

```
sou.zhaopin.com/?jl=530&kw=python&p=1
   │  EdgeOne 校验 TLS/HTTP2 指纹
   ▼  302 重定向 (服务端完成 kw 编码)
www.zhaopin.com/sou/jl530/kw01O00U80EG06G03F01N0/p1
   │  SSR HTML (__INITIAL_STATE__.positionList 含 20 条职位)
   ▼
纯 HTML 解析即可取数, 无需额外 JSON 接口
```

### 关键防线与结论 (Phase 2/3/4)

| 项 | 发现 | 结论 |
|----|------|------|
| **访问门控** | 腾讯云 **EdgeOne** Bot 验证（`Security Verification` 页，含 `TencentEOCaptchaWidget`） | 判断依据是 **TLS/HTTP2 指纹 (JA3/JA4)**，非 cookie。注入浏览器 cookie 仍被拦截 |
| **kw 编码** | `python→01O00U80EG06G03F01N0`, `java→01L00O80EO062`（确定性、会话间稳定） | 编码在 **服务端 302 Location** 完成，客户端跟随重定向即可，**无需自己实现** |
| **数据来源** | 职位列表在 SSR HTML 的 `__INITIAL_STATE__` 内联 JSON | 直接解析，不依赖 `fe-api` |
| **fe-api 动态参数** | `_v`, `x-zp-page-request-id`, `x-zp-client-id` | 是 `fe-api.zhaopin.com/c/i/*` 请求参数（筛选配置接口用），本方案不依赖 |
| **瑞数 cookie** | `FSSBBIl1UgzbN7NO/NS/NT` 存在但非访问门槛 | 浏览器直接访问也无需 EO-Bot-Captcha-Token；requests 无 cookie 被拦是因 TLS 指纹 |
| **绕过手段** | `curl_cffi` `impersonate="chrome"` 模拟 Chrome TLS 指纹 | **唯一且充分的绕过**（已验证多关键词/多城市/翻页） |

### 采集器方案

**纯 Python**（`curl_cffi` + SSR HTML 解析），无需 JS helper、无需浏览器。

```
python main.py --kw python,java --jl 530 --pages 3
```

## 目录结构

```
1-zhilian-scrawler/
├── main.py               # CLI 采集器入口
├── config/settings.py    # 城市代码映射
├── utils/
│   ├── http_client.py    # curl_cffi chrome 指纹客户端 (限速/重试)
│   ├── parser.py         # SSR HTML -> 职位数据解析
│   └── output.py         # CSV 输出
├── tests/                # 固定输入自检 + 真实协议测试
├── tools/                # CloakBrowser 侦察脚本 (capture_*.py)
├── js_reverse_cache/     # 侦察素材 (HTML/JS/网络样本)
└── output/               # 采集结果 CSV
```

## 使用

```bash
pip install -r requirements.txt

# 采集: python 北京(530) 3页
python main.py --kw python --jl 530 --pages 3

# 多个关键词 + 中文城市
python main.py --kw python,java,golang --city 北京 --pages 5 --output output/zhaopin.csv

# 测试
python -m pytest tests/ -v                          # 固定输入自检
ZHAOPIN_NETWORK_TEST=1 python -m pytest tests/ -v   # 含真实网络测试
```

## 逆向状态

- [x] Startup gate: 单 CloakBrowser 基线（Camoufox/js-reverse/chrome-devtools 缺失）
- [x] Phase 1: 确认真实请求路径（302 重定向 + SSR）
- [x] Phase 2: 动态字段分类（TLS 指纹门控 / 服务端 kw 编码）
- [x] Phase 3: 定位突变点（EdgeOne 指纹；编码在服务端）
- [x] Phase 4: 离线重建（curl_cffi chrome 指纹，无需本地编码）
- [x] Phase 5: 重复性验证（多关键词/多城市/翻页，positionCount 稳定）

## 风控说明

- 采集含限速（随机 1.2~2.5s/请求）+ 重试 + EdgeOne 拦截检测
- 高频采集可能触发 EdgeOne 频控，命中后降速重试
- 仅用于合法合规的数据采集与逆向研究

> 私有仓库：逆向签名算法代码不公开。如需公开请手动切换 GitHub repo visibility。
