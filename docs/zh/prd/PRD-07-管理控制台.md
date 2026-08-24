# PRD-07 · 管理控制台（MnemoSeed Console）

> 对应设计文档：[07-管理控制台](../design/07-管理控制台.md)
> 版本：v2.0 · 2026-08-13
> 里程碑：console-COMPLETE（读写齐全，页面 ①–⑩）是**营销前硬闸门**——console-COMPLETE + CLI 对等 + onboard 全部通过前，不得制作营销演示视频。排程：W1 ∥ W2 ∥ PRD-04；W3 在 PRD-04 落地后 · v2.0 预估 20d（W1 8d、W2 7d、W3 5d）

## 1. 目标

给"先手动再自动"纪律提供审查界面，让每条记忆的内容、权重、溯源、版本、使用记录全部可见可管——透明从口号变成界面。v2.0 补全写侧：console 的每个动作都即时生效、全留痕、且可通过 CLI 脚本化（能力对等）。

## 2. 范围

- **In（M1 只读核心）**：FastAPI 托管静态 SPA、localhost 隐式认证、Dashboard / Profiles / Memory Browser / Memory Detail / Dream 面板 / Conflicts 收件箱
- **In（console-COMPLETE）**：写操作（forget（tombstone）/ pin（never_decay）/ 调权重 / 解冲突 / dream --once / 自动触发器开关 / profile 创建-重命名-归档 / token 签发-吊销）、Audit Log、Settings、Graph View、ConfigWriteService 支撑的写入（FR-7.11）、CLI 对等（FR-7.12）、onboard 共享后端（FR-7.13）
- **Out**：Anima 面板 ⑪（FR-7.10，进阶模块）、多席位/license 的 Users 功能（design/06 §2.7——激活锁定）、云端超管平面、云端多租户 console（PRD-05 阶段复用同一前端切 baseurl）

## 3. 功能需求（console-COMPLETE）

| ID | 需求 | 优先级 |
|---|---|---|
| FR-7.1 | daemon 托管 `/console` 静态 SPA + `/api/v1/*` REST；`mnemoseed console` 打开浏览器 | P0 |
| FR-7.2 | Dashboard：状态机当前态、积分池水位、watermark、待巩固/needs_reconcile/pending 计数、token 用量按模型分组 | P0 |
| FR-7.3 | Profiles：列表/创建/重命名/归档；token 签发与吊销；绑定 agent 清单；记忆规模统计 | P0 |
| FR-7.4 | Memory Browser：短期（分片）/长期（节点）双 tab，按时间/项目/工具/实体/cue/Tier/decay 区间过滤 | P0 |
| FR-7.5 | Memory Detail 档案页：verbatim↔三元组对照、provenance 全时间线、版本链 diff、权重全量（decay 曲线投影、S 三分量、confidence、强化次数）、召回命中统计、全部标记位 | P0 |
| FR-7.6 | Dream 面板：待清算队列、运行历史（turn_range/模型/tokens/成本/分流计数/中断标记）、**提炼质量审查界面**（原始分片↔提炼物逐条对照，接受/拒绝/标幻觉）、dream --once 触发按钮、自动触发器开关（默认关） | P0 |
| FR-7.7 | Conflicts 收件箱：矛盾双方成对展示 + 四分支处理（强化/共存划界/作废/挂起），处理写回版本链 | P1（M1 末） |
| FR-7.8 | Graph View：**手写 three.js instanced 图层**——节点一次 `THREE.Points` 自定义 shader 绘制、边一次 `InstancedMesh` 四边形、top-60 中心性节点 canvas-sprite 标签、`Raycaster` 拾取、预计算聚类布局（2026-08-12 拍板；基准证据 [docs/bench/graphview-three-results.md](../../bench/graphview-three-results.md)，可运行工件 `.bench/graphview-three/`（本地、不入库），2026-08-13）；节点透明度 = decay_weight（遗忘可视化）、颜色 = 类型、大小 = 中心性、边粗细 = 权重；过滤 profile/类型/时间/Tier；点击进档案页；最低硬件上 5k 节点保持 ≥30 fps（NFR-7.2 v2）。**已实现（2026-08-15）**：页面 ④ 位于 `/console/#/graph`——按 docs/bench 证据采用 **vendored** three.js instanced 图层（`three.module.js` 置于 `/console/vendor`，永不走 CDN）；批量边来自 `list_edges`，图驱动缺该能力时按附录 C 显示降级提示 | P0（console-COMPLETE） |
| FR-7.9 | 全部写操作进 Audit Log（actor ∈ console\|cli\|mcp）；Audit Log 页面带过滤分页 | P0（console-COMPLETE） |
| FR-7.10 | Anima 面板（进阶模块，不在首发）：特质雷达图（轴数随 schema 不锁死六轴；顶点=mean，误差带=width 不确定性可视，允许手动微调）；白话创建（自然语言描述 → 模型量化生成模板）；核心实线 + 染层当前表现虚线叠加；跨 profile 链接/换绑入口 + 换绑触发 re-dye 确认；drift_history 时间轴回放（design/09 §7） | 进阶 / Out |
| FR-7.11 | 每条 console 写入与设置变更都由 **daemon 独占的 ConfigWriteService**（唯一配置写入者）支撑：registry → 校验 → 外科式 toml 补丁 → 版本化 meta-store 记录（既有 `set_config`/`rollback_config` 端口）→ 带 actor 归属（console\|cli\|mcp）的审计 → live-apply 或 restart-required 标记；config.toml 降级为生成镜像——registry 键以 meta store 为准（升级时 store 为空则一次性审计导入 `config_import`）；文件被手改按 mtime/hash 侦测，DB 胜出：镜像被重写并记 `config_mirror_drift` 告警 + 审计条目（原 `config_rebaseline` 语义作废）；**配置权限系统级**：⑧/⑨ 写操作仅 owner/admin 级（自托管 = owner 账号；商业多用户 license = admin 级、作用于所有用户；SaaS = 云 Admin Plane、作用于所有用户），不是用户个人设置 | P0 |
| FR-7.12 | **CLI 能力对等**：console 的每个动作都能用 `mnemoseed` CLI 脚本化（JSON/表格输出）；交互式可视化 console 独有，但都有 CLI 数据等价物（如 `graph export --json`）；`mnemoseed config set\|get\|rollback` 在 console↔CLI 间往返一致；新增 `mnemoseed audit` 动词；对等矩阵入库 docs；CLI 配置操作走 REST 客户端（仅 REST；`--force` 离线逃生只打印 "not audited (daemon down)"，仅限 loopback baseurl） | P0 |
| FR-7.13 | **Onboard 共享后端**：console 设置向导与 CLI `mnemoseed onboard` 动词是同一个后端服务的两个前端（细节落 PRD-06）；console 绝不自己实现 onboarding 流程 | P0 |

## 4. 非功能需求

| ID | 需求 |
|---|---|
| NFR-7.1 | 默认仅监听 localhost；非 localhost 访问必须显式开启 + admin token |
| NFR-7.2 (v2) | 10 万级记忆规模下浏览页首屏 < 1s；Graph View 在 5k 节点保持 **≥30 fps**，最低硬件 = **WebGL2 + 独显级 GPU**（iGPU 未实测）；基准证据见 design/07 §④（2026-08-13） |
| NFR-7.3 | console 是纯客户端——关掉页面不影响 daemon 任何功能 |

## 5. 验收标准（闸门 AC 集）

| ID | 验收标准 |
|---|---|
| G-AC1 | Console 写齐全：forget（tombstone）/ pin（never_decay）/ 调权重 / 解冲突 / dream --once / 自动触发器开关 / profile 创建-重命名-归档 / token 签发-吊销——全部会话内即时生效且留痕 |
| G-AC2 | ⑧ 配置全部两个梦境角色（deep_reflection / short_increment）；持久化前连通性实测通过；UI 只暴露 env-var **名称**（UI 出现字面 key 即测试失败）；版本化 + 可回滚 |
| G-AC3 | ⑨ Settings（w₁/w₂/w₃、按类型 λ、top-k、token 预算、积分池阈值）校验 / 持久化 / 审计 / 回滚；每 key 的 live-vs-restart 分类有文档；有数据时存储驱动切换被禁用 |
| G-AC4 | Graph View（three.js）最低硬件上 5k 节点保持 ≥30 fps；透明度 = decay_weight、颜色 = 类型、大小 = 中心性、边粗细 = 权重；点击 → Memory Detail；过滤 profile/类型/时间/Tier |
| G-AC5 | CLI 对等矩阵入库；console 每个动作都有 CLI 对应物且状态迁移一致；审计 actor 归属正确（cli/console/mcp）；`config set/get/rollback` console↔CLI 往返一致 |
| G-AC6 | 干净机器 onboard：owner → preset → LLM 向导（连通性实测）→ 宿主链接（备份 + diff + 确认）→ 开机自启 → doctor 全绿；console 向导用同一后端；跳过 LLM → 仅捕获 daemon |
| G-AC7 | 审计完整性：脚本化序列（forget + 调权重 + 切模型 + 解冲突 + 翻自动触发器）在 Audit Log 中归属正确 |

## 6. 任务拆分

### W1 · ConfigWriteService + console 写操作 + Audit + Settings + ⑧（8d）——与 PRD-04 并行

1. `core/configwrite` —— daemon 独占唯一配置写入者：registry → 校验 → 外科式 toml 补丁 → 版本化 meta-store 记录（`set_config`/`rollback_config` 端口）→ 审计（actor ∈ console|cli|mcp）→ live-apply/restart-required 标记；registry 键 DB 为主 + 一次性 `config_import`；mtime/hash 漂移侦测 → 镜像重建 + `config_mirror_drift` 审计（2d）
2. `console/write` —— forget（tombstone）/ pin（never_decay）/ 调权重 / 解冲突 / dream --once / 自动触发器开关 / profile 创建-重命名-归档 / token 签发-吊销，全部会话内留痕（2d）
3. `console/audit` —— Audit Log 页面 + 过滤分页（0.5d）
4. `console/settings` + `console/models` —— ⑨ Settings（w₁/w₂/w₃、按类型 λ、top-k、token 预算、积分池阈值；每 key live-vs-restart 表；有数据时驱动切换禁用）+ ⑧ Models & Routing（两角色、仅 env-var 名称、持久化前连通性实测、版本化 + 可回滚）（1.5d）
5. `console/integration` —— 跨界面审计完整性扫描（G-AC7 脚本）+ 与 CLI 套件联合的闸门演练（2d）

### W2 · CLI 对等动词（7d）——与 PRD-04 并行

6. `cli/parity` —— 核心动词走 daemon REST 客户端：`console` / `status` / `link` / `unlink` / `recall` / `remember` / `dream --once` / `export` / `diff` / `forget` / `audit`；JSON/表格输出（3d）
7. `cli/config` —— `config set|get|rollback` 走 ConfigWriteService REST（FR-7.11）；仅 REST，`--force` 离线逃生打印 "not audited (daemon down)"，仅限 loopback baseurl（1.5d）
8. `cli/onboard` —— `mnemoseed onboard` 引导式聚合（共享 onboard 后端；可跳过 + 可续跑；FR-6.10）（1.5d）
9. `cli/matrix` —— 对等矩阵入库 docs + console↔CLI 往返闸门检查（G-AC5）（1d）

### W3 · Graph View + 演示造数（5d）——PRD-04 落地后

10. GraphStore 端口扩展：`list_edges(filter, page)` + `GRAPH_EDGE_LIST` 能力（值 `graph.edge_list`；per PRD-08 附录 B 修订 v1.1）（1d）
11. `console/graph` —— 手写 three.js instanced 图层 Graph View（THREE.Points 节点 / InstancedMesh 边 / canvas-sprite top-60 标签 / 拾取 / 预计算聚类布局）+ 过滤（profile/类型/时间/Tier）+ 点击 → Detail（2d）
12. 为衰减图谱演示造数（各类型/Tier 间 decay 权重方差，供营销演示）+ 最低硬件 GPU 复测（NFR-7.2 v2），含端口扩展 e2e 暴露的修复（2d）

合计 ≈ 20d。

## 7. 依赖

- PRD-01/02/03/04 全部（console 是它们的观测面）；W1/W2 与 PRD-04 并行，W3 在 PRD-04 落地后
- PRD-06（login/token 身份模型，admin token 复用；onboard 共享后端服务）
- PRD-08 附录 B 修订 v1.1（[PRD-08](../prd/PRD-08-M0地基.md) GraphStore `list_edges(filter, page)` + `GRAPH_EDGE_LIST` 能力，值 `graph.edge_list`）——W3 前置
