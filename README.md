# 分布式网页抓取调度平台

面向分布式网页抓取任务调度、节点协同、请求分配、反爬策略编排、数据校验入库与抓取质量大盘的采集工程管理平台。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 采集节点 | `worker` | 工作节点 | 节点编号、主机地址、并发槽位数 |
| 请求分配 | `dispatch` | 分配策略 | 策略编号、目标域名、分配算法 |
| 抓取作业 | `crawljob` | 作业定义 | 作业编号、目标站点、起始URL |
| URL过滤 | `urlfilter` | 过滤规则 + 规则版本快照 | 规则编号、包含模式、排除模式、版本号、发布时间 |
| URL准入 | `urladmission` | 准入批次、准入记录、抓取消费侧清单 | 批次编号、规范化URL、canonical key、命中依据、抓取状态 |
| DNS解析 | `dnsresolver` | 解析配置 | 配置编号、域名模式、DNS服务器 |
| 内容提取 | `contentextract` | 提取器 | 提取器编号、内容类型、正文选择器 |
| 存储策略 | `storagestrategy` | 存储方案 | 方案编号、存储类型、连接参数 |
| 渲染任务 | `rendertask` | 渲染作业 | 作业编号、目标URL、浏览器版本 |
| 采集质量 | `qualityreport` | 质量报告 | 报告编号、作业编号、总URL数 |
| 带宽控制 | `bandwidth` | 带宽策略 | 策略编号、节点标识、下载带宽上限 |
| 重试队列 | `retryqueue` | 重试条目 | 条目编号、失败URL、失败原因码 |
| 翻页管理 | `pagemarker` | 翻页规则 | 规则编号、目标站点、翻页类型 |
| 增量标记 | `incrementmark` | 增量水位 | 水位编号、目标源、增量字段名 |
| 配置同步 | `configsync` | 同步任务 | 任务编号、源节点、目标节点 |
| 采集API | `scrapeapi` | API定义 | API编号、API名称、请求路径 |
| 数据抽样 | `datasample` | 抽样方案 | 方案编号、数据源、抽样方式 |
| 采集告警 | `alertpolicy` | 告警策略 | 策略编号、监控指标、触发条件 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

### URL 准入管线（`urlfilter` + `urladmission`）

候选 URL 进入采集范围的完整链路：候选 URL → 规范化 → 规则版本评估 → 去重清单 → 抓取消费侧。

- **规范化**（`app/services/url_rules.py`）：协议/主机小写、默认端口折叠、尾斜杠折叠、
  查询参数排序、可配置参数剥离、片段去除，产出唯一 `canonical_key` 作为去重口径；
  大小写、参数顺序、尾斜杠差异不会制造重复页面。
- **规则版本快照**：规则发布时冻结白名单、路径黑名单、包含/排除模式、参数剥离、
  大小写与尾斜杠策略等全部口径并立即编译模式；非法模式发布即明确失败，不落库不生效
  （fail-closed）。快照不可变，换版只影响新批次，旧批次永久保留原规则依据。
- **准入结果与去重**：同一 `canonical_key` 在批次内只准入一次，重复项保留拒绝明细；
  非法 URL、未命中白名单、命中黑名单/排除模式均给出明确失败原因，绝不默认放行。
  每条记录保留规范化前后值与逐环节命中依据，可从规则版本与命中记录复核抓取范围。
- **幂等与并发**：批次以「规则版本 + 候选集」哈希为幂等键，进程内加锁串行化，
  重复提交（含并发）只生效一次，不会产生第二份结果。
- **抓取消费侧**：准入结果驱动写入 `crawl_consumer_list`（重复同步幂等）；
  消费侧通过 `write-back` 接口回写实际抓取状态，准入记录同步更新。

领域：数据工程
子方向：数据采集、爬虫与RPA
