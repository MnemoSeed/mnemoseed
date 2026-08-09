# PRD-08 · M0 地基（骨架 + 存储接口 + Schema 冻结）

> 对应设计文档：[03-存储与检索](../design/03-存储与检索.md) §1/§3、[06-接入与安装体验](../design/06-接入与安装体验.md) §3
> 里程碑：M0（路线图 PRD-00 的 m0a + m0b）· 预估 16 天（v2 盲审修订）
> 性质：纯地基，零用户可见功能——但它冻结的东西（schema、接口契约）之后改一次疼一次，所以本 PRD 的审查标准是全部 PRD 里最严的。
> v2 修订：经 blind-reviewer 独立审查（15 项发现），增补接口方法清单（附录 B）、降级行为表（附录 C）、schema 冻结字段补全（profile 隔离 / 结构化 turn 边界 / 标记位 / 用量计数 / 稀疏向量表示）。

## 1. 目标

1. core repo 从零到可开发状态：包结构、CI、测试框架、compose 骨架；
2. 四个存储端口接口定稿（方法级清单见附录 B），每个接口实现**内嵌默认 + 第二驱动**，用契约测试实证接口可移植性；
3. **Schema v1 冻结**（清单见附录 A）：全部表/字段定义落地为代码迁移文件，此后变更只能走迁移机制，不允许手改库。

## 2. 范围

- **In**：core repo 骨架与 CI；VectorStore / GraphStore / MetaStore / Embedder 四接口 + 驱动注册表（支持按层命名多实例）；embedded 默认栈四驱动（LanceDB / SQLite-Graph / SQLite-Meta / bge-m3 ONNX）；Postgres 系三驱动（pgvector / pg-graph / pg-meta）+ OpenAI 兼容 Embedder；capability flags 校验与降级行为表；schema 迁移机制；docker-compose 骨架；embedded 单进程**骨架**（daemon 起停 + `/healthz`，无任何业务逻辑——daemon 业务面归 PRD-06 FR-6.2，两处不重复建设）
- **Out**：任何五阶段管线逻辑（捕获/梦境/检索/调和/衰减——PRD-01~04）；MCP 网关实现（mcp repo）；宿主接入（PRD-06）；console（PRD-07）；驱动性能调优（性能验收归 PRD-03 NFR，M0 契约绿灯 ≠ 性能绿灯）

## 3. 已拍板决策

| # | 决策 | 理由 |
|---|---|---|
| D1 | SQLite-Graph **自建邻接表**（nodes + edges 两表），不引现成图库 | 查询模式固定（1-2 hop 遍历/共现边/版本链），零依赖、schema 自主；将来换库只是加一个驱动 + 一次性导出导入，verbatim 通道不动所以最坏情况也可重建 |
| D2 | Postgres 图侧**纯关系表模拟**（与 SQLite 版同构），不用 Apache AGE | 任何托管 PG 都能跑，云端不挑供应商；同一接口不维护两套查询逻辑 |
| D3 | capability flags **最小可用集**（11 个，见 FR-8.6） | 冻结的是校验机制，不是清单；不给未设计的功能提前锁死命名 |
| D4 | **保持双 repo**（core Python / mcp Node），不切 monorepo | 接入层本来就可替换；独立 CI、独立版本节奏；零代码阶段切结构只有成本没有收益 |
| D5（v2 新增） | **profile 隔离走钢印字段**：chunks 加 `profile_id`，靠 `vector.metadata_filter` 过滤；不引入"每层多实例向量库"的配置复杂度 | 与 nodes 的 `profile_id` 对齐；PRD-06 身份模型每次调用显式携带 profile_id，天然匹配 |
| D6（v2 新增） | **Tier-3 隔离图谱 = 第二个 GraphStore 命名实例**（embedded 下独立 SQLite 文件，PG 下独立 schema）；注册表支持按层命名多实例（`graph.main` / `graph.isolated`） | design/02 §5 承诺的是物理隔离，分区键降级会破坏"不可反向污染"的叙事；接口不变，只是注册表多一个名字 |
| D7（v2 新增） | **契约测试用确定性合成 embedder**（固定维度的 hash 伪向量），bge-m3 真实推理单独走带模型缓存的冒烟测试 | CI 不可能每个 PR 拉 ~543MiB 模型（NFR-8.2 ≤5min）；可移植性实证验的是接口行为不是向量质量 |

## 4. 功能需求

| ID | 需求 | 优先级 |
|---|---|---|
| FR-8.1 | core repo 骨架：uv 管理 src-layout 包、pytest、ruff、mypy、GitHub Actions CI（lint + typecheck + test，PR 必过；bge-m3 模型文件走 CI 缓存） | P0 |
| FR-8.2 | 四端口接口定义为 Protocol，**方法清单以附录 B 为准**；接口配驱动注册表，config.toml 按层选驱动且**支持按层命名多实例**（如 `graph.main` / `graph.isolated`）；preset（embedded/docker/custom）+ 逐层覆盖，`STORAGE_MODE` 保留为 preset 快捷方式；preset 枚举可扩展（cloud 预留），不写死 | P0 |
| FR-8.3 | embedded 驱动四件：`lancedb_embedded` / `sqlite_graph`（自建邻接表）/ `sqlite_meta` / `bge_m3_onnx`（模型文件首次运行下载 ~543MiB（int8 量化 ONNX，实测），进度可见；另有 `synthetic` 测试 embedder） | P0 |
| FR-8.4 | 第二驱动：`pgvector` / `pg_graph`（纯关系表，与 sqlite_graph 同 schema 同查询）/ `pg_meta`；Embedder 第二驱动 = `openai_compatible`（任意兼容端点，**只出 dense**，声明缺 `embed.sparse_output`） | P0 |
| FR-8.5 | **接口契约测试套件**：与驱动无关的行为测试，覆盖附录 B 每个方法（附"方法 ↔ 契约测试"映射表），对 embedded 与 pg 两套驱动各跑一遍；另含 SQLite/PG **迁移对账断言**（同一 schema_version 序列、同字段定义，两侧比对零差异） | P0 |
| FR-8.6 | capability flags 最小集（11 个）：`vector.hybrid_search` / `vector.metadata_filter` / `vector.snapshot` / `graph.traverse_2hop` / `graph.version_chain` / `graph.cooccurrence_edges` / `meta.transaction` / `meta.concurrent_readers` / `embed.local_inference` / `embed.batch` / `embed.sparse_output`。缺能力的驱动组合：启动拒绝或走**明示降级**，降级行为表以附录 C 为准（代码与 config 文档同步） | P0 |
| FR-8.7 | **Schema v1 冻结**（清单见附录 A）：全部结构落地为迁移文件（SQLite 与 PG 各一套，同一 schema_version 序列）；`schema_version` 表 + 纯前向 up 迁移机制；钢印 `cues.host` / `cues.task` 及 `profile_id` / `session_id` / `turn_start` / `turn_end` 字段**可空不可缺席** | P0 |
| FR-8.8 | docker-compose 骨架（docker preset：`core + vector(pgvector) + pg + embed` 四服务，ollama 可选 profile），每服务 `/healthz`；embedded 单进程 `mnemoseed up` 一条命令起 daemon 骨架（全部驱动内嵌，无外部依赖，无业务逻辑） | P0 |

## 5. 非功能需求

| ID | 需求 |
|---|---|
| NFR-8.1 | embedded 模式冷启动（**bge-m3 已缓存**）普通开发机 ≤ 10s，口径 = boot→/healthz 绿 + 首次 embed 完成（模型为惰性加载，boot 本身不含模型加载；两项分段计时均须报出）；首次模型下载不计入，单独标注进度；`/healthz` 响应 < 100ms |
| NFR-8.2 | 契约测试全套（双驱动，合成 embedder）CI 运行 ≤ 5min；bge-m3 冒烟测试单独 job 且走缓存 |
| NFR-8.3 | 全部公开代码/注释/接口命名英文；无日期、人名、决策记录进代码 |

## 6. 验收标准

- AC-1：干净 clone → `uv sync` → `uv run pytest` 全绿；CI 在 PR 上实际跑过并拦截过一次故意引入的失败（演示 CI 有效）；
- AC-2：`docker compose up` 一键起全栈，全部服务 `/healthz` 绿（响应 < 100ms 实测）；`mnemoseed up` embedded 单进程起 daemon 且健康检查绿；bge-m3 已缓存冷启动计时 ≤ 10s；
- AC-3：契约测试套件对 embedded 与 pg 双驱动**各跑一遍全通过**，附录 B 每个方法至少一条契约测试（映射表随测试报告输出）；
- AC-4：降级行为表（附录 C）中至少 3 条真实子集组合实测：`embed.sparse_output` 缺失（openai_compatible）→ 检索降级 dense-only + 启动警告；`vector.snapshot` 缺失 → 梦境快照语义退化为 turn_range 逻辑隔离 + 警告；`meta.transaction` 缺失 → **启动拒绝**。全程无静默路径；
- AC-5：迁移机制演示：同一迁移（schema 1→2，加一个无害列）在 SQLite 与 PG **两侧都执行**，版本序列一致、已有数据（含 provenance.history 与版本链）零丢失、两侧数据行逐一比对零差异；
- AC-6：附录 A 冻结清单逐条核对——每个字段在迁移文件中存在、类型一致（含 `vector_sparse` 的结构化表示）、钢印预留字段在场（可空）、双图谱命名实例各自可建库。

## 7. 任务拆分（每任务 = 一次 programmer 派遣 + verifier 验收）

| # | 任务 | 预估 |
|---|---|---|
| 1 | repo 骨架 + CI（FR-8.1） | 1.5d |
| 2 | 四接口 Protocol（按附录 B）+ 注册表（含命名多实例）+ preset 解析 + capability 校验（FR-8.2/8.6 接口侧） | 2d |
| 3 | SQLite-Graph（双实例：main + isolated）+ SQLite-Meta + schema v1 迁移机制与对账断言（FR-8.3 半 / FR-8.7） | 3d |
| 4 | LanceDB + bge-m3 ONNX + synthetic embedder（FR-8.3 半） | 2d |
| 5 | Postgres 系三驱动 + openai_compatible embedder（FR-8.4） | 2.5d |
| 6 | docker-compose 骨架 + 健康检查 + embedded 单进程骨架（FR-8.8） | 1.5d |
| 7 | 契约测试套件（含迁移对账）+ 全部 AC 演示（FR-8.5 / §6） | 3d |

合计 15.5d（+集成缓冲 0.5d ≈ **16d**，路线图 m0b 相应调整为 9d）。

## 8. 依赖

- 无上游依赖（第一个开工的 PRD）；
- **阻塞全部后续 PRD**（01/02/03/04/06/07 全部站在接口与 schema 上）。

## 9. 已识别残留风险（M0 不解决，登记备查）

- **SQLite 断电耐久**：score_pool/watermark 的并发正确性有契约测试覆盖，但 embedded 单进程断电崩溃恢复无 AC——M1 捕获链路（PRD-01）上线前补崩溃恢复测试；
- **bge-m3 ONNX 分发实测**：~543MiB（实测 569.7MB，int8 量化）+ ONNX runtime 依赖链与 TTFM < 3min 的真实适配，需 PRD-06 安装流程实测验证；
- **LanceDB 十万级 p95**：M0 契约测试不暴露真实规模性能，PRD-03 NFR-3.1（300ms）届时独立验收。

---

## 附录 A · Schema v1 冻结清单

> 冻结 = 此后变更只能写新迁移，禁止手改。字段级细节以迁移文件为准，本清单是审查对照表。
> 来源：design/03 §3 erDiagram + design/01 §1 钢印 + PRD-01 FR-1.6 + v2 盲审补全。

### A.1 海马体（VectorStore / LanceDB 表 `chunks`）

| 字段 | 类型 | 说明 |
|---|---|---|
| chunk_id | uuid PK | |
| text | string | verbatim 原文，永不被有损处理 |
| vector_dense | float[] | bge-m3 dense 输出 |
| vector_sparse | **struct {indices: int[], values: float[]}** | bge-m3 sparse 输出约 25 万维仅少数非零，禁止存稠密数组 |
| profile_id | string | profile 命名空间（D5），检索强制过滤 |
| session_id / turn_start / turn_end | string / int / int | 结构化 turn 边界（快照界定、安全清空、积分池事件都依赖，可空不可缺席）；provenance.source_ref 只作人类可读串 |
| cognitive_tier | int | 1 / 3 |
| model_id / anima_id | string | 写入时模型与在任灵魂 |
| cues | struct | `project / host* / task* / tools_used[] / time_bucket / emotion_valence / entities[]`（* = 编码情境预留，可空不可缺席） |
| provenance | struct | `asserted_by / source / source_ref / confidence / asserted_at / history[]`（append-only） |
| score | struct | `emotion / novelty / causal / total` |
| decay_weight | float | 默认 1.0 |
| last_reinforced / ingested_at | datetime | Freshness Guard 依赖 ingested_at 过滤 |
| consolidated | bool | 梦境清空快照后置位，加速衰减 |
| peripheral_gaps | bool | 高唤醒周边信息缺口（design/01 §1.6） |
| needs_reconcile | bool | 近重复 0.85–0.9 区间置位（PRD-01 FR-1.8） |
| hit_count / last_hit_at / reinforce_count | int / datetime / int | 用量统计（console Detail"使用情况"区；不从 audit_log 派生，防事件流膨胀） |

### A.2 皮层（GraphStore / SQLite 与 PG 同构；`graph.main` 与 `graph.isolated` 两实例同 schema）

- **nodes**：`id PK / type / profile_id / payload JSON / decay_weight / conflict_flag / conflict_group / needs_reconcile / pending_consolidation / peripheral_gaps / valid_from / valid_to / last_reinforced / hit_count / last_hit_at / reinforce_count / provenance JSON / created_at`
  - `conflict_group`：冲突双方共享同一组 ID——成对返回（FR-3.6）与 Conflicts 收件箱靠它定位对方，单 bool 不够；
  - 三个流程旗标（needs_reconcile / pending_consolidation / peripheral_gaps）是 PRD-01/02/03 与 console Detail 页共同依赖的负载字段，必须在冻结内。
- **edges**：`id PK / src / dst / rel / weight / provenance JSON`（共现边 = `rel='cooccur'` + weight 计数）
- **node_versions**：append-only 版本链（`node_id / version / payload 快照 / changed_at / superseded_by`）——as_of 双时态查询的物理基础
- **节点类型枚举（v1 冻结）**：`USER / HABIT / PREFERENCE / ANIMA / INTENTION / CONSTRAINT / EPISODE / SKILL_SEQUENCE / DECISION / PROJECT / TOOL`
- 各类型 payload 字段按 design/03 §3 erDiagram；PREFERENCE 含 `valence / prior_width / trait_anchor / evidence_chain`；ANIMA 含 `core_traits / dye_layer / idiographic_notes / drift_history`

### A.3 MetaStore（SQLite 与 PG 同构）

- **schema_version**（迁移机制自身）、**profiles**、**tokens**（凭证签发/吊销）、**score_pool**（watermark + 积分池水位，要求事务原子更新；事件含 turn_range）、**config**（版本化 + 可回滚）、**audit_log**（append-only，读写事件流；保留与聚合策略：明细 90 天滚动 + 聚合计数永久——**用量计数不从 audit_log 派生**，走 A.1/A.2 的计数字段）、**dream_runs**（梦境运行历史：turn_range/模型/tokens/成本/分流计数/中断标记，console Dream 面板与幂等恢复依赖）

### A.4 显式不冻结

- 各驱动内部索引结构（LanceDB 索引参数、PG 索引）——实现细节可演进；
- capability flags 清单——可后加，校验机制才是冻结对象；
- preset 枚举——cloud preset 预留扩展点。

---

## 附录 B · 接口方法清单（契约测试映射基准）

> 每个方法至少一条契约测试（AC-3）。签名细节以代码为准，此处冻结**方法面与语义要求**。

### B.1 VectorStore

| 方法 | 语义 | 消费方 |
|---|---|---|
| upsert_chunk / get_chunk / delete_chunk | 写入 / 按 id 取单片 / forget_this 删除 | 捕获、console Detail、PRD-03 |
| search(dense, sparse?, filter, top_k) | 混合检索 + metadata 过滤（profile_id / decay_weight 下限 / 时间区间） | 检索 |
| near_duplicate(vector, threshold, profile_id) | 近重复探测，支持 0.9 / 0.85 双阈值；profile_id 必填（D5 隔离） | 捕获 FR-1.8 赫布强化 |
| snapshot_read(filter) | 梦境只读快照；无 snapshot 能力时退化 turn_range 逻辑读 | 梦境引擎 |
| mark_consolidated(chunk_ids) | 批量置 consolidated | 梦境清空 |
| purge_range(session_id, turn_start, turn_end) | 按快照范围安全清空，两端互不干扰 | 梦境 FR-2.x |
| update_weights(updates[]) | 批量写 decay_weight / last_reinforced / reinforce_count | 衰减与强化回弹 |
| update_chunk_state(chunk_ids, hit_increment?, needs_reconcile?) | 批量写使用计数（hit_count / last_hit_at）与 needs_reconcile 置位/清除；hit_increment>0 时同时刷新 last_hit_at | 检索命中计数、捕获 FR-1.8 疑似矛盾标记 |
| list_chunks(filter, page) | 过滤 + 分页列表 | console Browser |
| capabilities() | 自报能力集 | 启动校验 |

### B.2 GraphStore

| 方法 | 语义 | 消费方 |
|---|---|---|
| upsert_node / get_node / list_nodes(filter, page) | 节点写读 / console 过滤分页 | 梦境、console |
| add_edge / bump_cooccurrence(a, b) | 关系边 / 共现边 +1 | 梦境、检索强化 |
| traverse(node_id, depth ≤ 2, filter) | 实体子图遍历 | 检索 |
| find_same_predicate(subject, predicate) | 同主同谓既有事实探测 | Reconcile 写入侧 |
| set_flags / clear_flags(nodes, flags) | needs_reconcile / pending_consolidation / conflict_group 置位与清除 | 捕获、检索、调和 |
| invalidate(node_id, valid_to) + append_version | 旧版本作废 + 版本链追加（原子） | Reconcile 再巩固 |
| versions(node_id) / diff(v1, v2) / timeline(node_id) | 版本链查询 / 任意两版 diff / 时间轴回放 | console Detail |
| as_of(timestamp, filter) | 时间点回放查询（双时态） | 检索 FR-3.9 |
| batch_update_weights(updates[]) | 批量衰减重算（10 万级 < 60s，NFR-4.1） | Decay |
| query_intentions(status, due_before) | pending INTENTION 到期查询 | 调度器 FR-3.15 |
| capabilities() | 自报能力集 | 启动校验 |

注：图谱中心性（重排公式 δ 项）由检索侧基于 traverse 结果端侧计算，M0 不引入独立中心性查询。

### B.3 MetaStore

| 方法 | 语义 | 消费方 |
|---|---|---|
| pool_add(points, turn_range) / pool_state() | 积分池原子累计 / 水位与 watermark 读取 | 捕获 FR-1.5 |
| advance_watermark(turn_range) | watermark 原子推进 | 梦境 |
| profiles CRUD / tokens issue / revoke | 身份与凭证 | PRD-06 |
| config get / set（版本化 + rollback） | 配置版本化 | console Settings |
| audit_append / audit_query(filter, page) | 审计 append-only 写 / 过滤分页读 | 全局 |
| dream_runs record / list | 梦境运行历史 | 梦境、console |
| schema_version get / migrate(up) | 迁移机制 | 安装与升级 |
| capabilities() | 自报能力集 | 启动校验 |

### B.4 Embedder

| 方法 | 语义 |
|---|---|
| embed(text) → {dense, sparse?} | 单条向量化；无 sparse 能力时缺省 |
| embed_batch(texts[]) | 批量 |
| capabilities() | `local_inference` / `batch` / `sparse_output` |

---

## 附录 C · 降级行为表（启动校验判据）

| 缺失能力 | 行为 | 级别 |
|---|---|---|
| `meta.transaction` | **拒绝启动**（积分池/watermark 原子性是硬需求） | 硬性 |
| `graph.version_chain` | **拒绝启动**（Reconcile/as_of 依赖） | 硬性 |
| `vector.metadata_filter` | **拒绝启动**（profile 隔离与 Freshness Guard 依赖） | 硬性 |
| `embed.sparse_output` | 混合检索降级为 dense-only，检索质量警告 | 降级 + 启动警告 |
| `vector.hybrid_search` | 同上（dense-only） | 降级 + 启动警告 |
| `vector.snapshot` | 梦境快照退化为 turn_range 逻辑隔离，隔离强度降级警告 | 降级 + 启动警告 |
| `graph.cooccurrence_edges` | 重排丢 ε 共现项，检索质量警告 | 降级 + 启动警告 |
| `meta.concurrent_readers` | console 读取串行化，并发性能警告 | 降级 + 启动警告 |
| `embed.batch` | 向量化逐条执行，吞吐警告 | 降级 + 启动警告 |

规则：硬性缺失 = 拒绝启动并打印缺失能力清单；降级 = 启动通过但必须打印明示警告并写入启动日志。任何路径不得静默。
