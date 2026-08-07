# PRD-07 · 管理控制台（MnemoSeed Console）

> 对应设计文档：[07-管理控制台](../design/07-管理控制台.md)
> 里程碑：M1 上只读核心（Dashboard / Memory Browser / Dream 面板 / Conflicts），M2 补全写操作与 Graph View · M1 部分预估 6 天

## 1. 目标

给"先手动再自动"纪律提供审查界面，让每条记忆的内容、权重、溯源、版本、使用记录全部可见可管——透明从口号变成界面。

## 2. 范围

- **In（M1）**：FastAPI 托管静态 SPA、localhost 隐式认证、Dashboard / Profiles / Memory Browser / Memory Detail / Dream 面板 / Conflicts 收件箱
- **In（M2）**：Graph View 可视化、写操作（forget/pin/调权重/解冲突/切模型）、Audit Log、Settings
- **Out**：云端多租户 console（PRD-05 阶段复用同一前端切 baseurl）

## 3. 功能需求（M1 部分）

| ID | 需求 | 优先级 |
|---|---|---|
| FR-7.1 | daemon 托管 `/console` 静态 SPA + `/api/v1/*` REST；`mnemoseed console` 打开浏览器 | P0 |
| FR-7.2 | Dashboard：状态机当前态、积分池水位、watermark、待巩固/needs_reconcile/pending 计数、token 用量按模型分组 | P0 |
| FR-7.3 | Profiles：列表/创建/归档；token 签发与吊销；绑定 agent 清单；记忆规模统计 | P0 |
| FR-7.4 | Memory Browser：短期（分片）/长期（节点）双 tab，按时间/项目/工具/实体/cue/Tier/decay 区间过滤 | P0 |
| FR-7.5 | Memory Detail 档案页：verbatim↔三元组对照、provenance 全时间线、版本链 diff、权重全量（decay 曲线投影、S 三分量、confidence、强化次数）、召回命中统计、全部标记位 | P0 |
| FR-7.6 | Dream 面板：待清算队列、运行历史（turn_range/模型/tokens/成本/分流计数/中断标记）、**提炼质量审查界面**（原始分片↔提炼物逐条对照，接受/拒绝/标幻觉）、dream --once 触发按钮、自动触发器开关（默认关） | P0 |
| FR-7.7 | Conflicts 收件箱：矛盾双方成对展示 + 四分支处理（强化/共存划界/作废/挂起），处理写回版本链 | P1（M1 末） |
| FR-7.8 | Graph View：Cytoscape.js 交互图谱，节点透明度 = decay_weight（遗忘可视化），点击进档案页 | P1（M2） |
| FR-7.9 | 全部写操作进 Audit Log；Audit Log 页面（M2） | P1 |

## 4. 非功能需求

| ID | 需求 |
|---|---|
| NFR-7.1 | 默认仅监听 localhost；非 localhost 访问必须显式开启 + admin token |
| NFR-7.2 | 10 万级记忆规模下浏览页首屏 < 1s，图谱视图 5k 节点流畅交互 |
| NFR-7.3 | console 是纯客户端——关掉页面不影响 daemon 任何功能 |

## 5. 验收标准

- AC-1：dream --once 全流程（审查队列 → 触发 → 逐条对照审查 → 接受/拒绝）全程在 UI 完成，不碰 CLI/JSON；
- AC-2：任取一条长期记忆，能回答"哪来的、被谁写的、改过几版、每次改了什么、现在权重多少、被召回过几次"；
- AC-3：构造一对矛盾记忆，Conflicts 收件箱出现并完成"情境共存"处理，版本链留下记录；
- AC-4：杀掉 console 静态服务，daemon 捕获/检索/梦境全部照常。

## 6. 任务拆分

1. `console/api` —— REST 端点（状态/记忆查询/梦境控制/冲突处理）（2d）
2. `console/web` —— SPA 骨架 + Dashboard + Memory Browser + Detail（2d）
3. Dream 面板 + 审查界面（1d）
4. Conflicts 收件箱 + e2e（1d）

## 7. 依赖

- PRD-01/02/03/04 全部（console 是它们的观测面）
- PRD-06（login/token 身份模型，admin token 复用）
