# PRD-03 · 混合检索与 MCP 网关（Retrieve + 上下文装配）

> 对应设计文档：[03-存储与检索](../design/03-存储与检索.md)
> 里程碑：M1 · 预估 10 天

## 1. 目标

对外暴露标准 MCP 工具集，对内实现并发双路混合检索 + 抗稀释装配。任何 MCP Host（Cursor / Cline / Windsurf）零改动接入。

## 2. 范围

- **In**：MCP 工具定义、线索提取器、双路并发检索、融合重排、token 预算闸、上下文装配
- **Out**：衰减权重计算本身（PRD-04）、云端多 Profile 隔离（PRD-05）

## 3. 功能需求

| ID | 需求 | 优先级 |
|---|---|---|
| FR-3.1 | MCP 工具集：`memory.recall` / `memory.remember`(显式 pin) / `memory.audit`(溯源查询) / `memory.timeline`(时间轴) / `memory.export`(全量导出可读格式) / `memory.forget_this`(用户显式删除权，GDPR 被遗忘权合规) | P0 |
| FR-3.1b | SessionStart 暖场注入：新 session 开启时主动推送"近期记忆摘要"开场上下文，而非被动等 recall（借自 Claude-Mem 生命周期 hook） | P1 |
| FR-3.2 | 线索提取：从当前对话前文解析实体/项目/工具/意图，生成检索 cues | P0 |
| FR-3.3 | 并发双路：Chroma 语义近邻（含线索过滤）+ 图谱实体子图 2-hop 遍历 | P0 |
| FR-3.4 | 融合重排公式 `α·语义 + β·线索重叠 + γ·decay_weight + δ·图谱中心性`，权重可配置 | P0 |
| FR-3.5 | 抗稀释硬闸：top-k ≤ 5、token 预算 ≤ 800（默认，可调），超预算丢尾部并返回 dropped_count | P0 |
| FR-3.6 | conflict_flag 记忆成对返回 + 显式标注 | P0 |
| FR-3.7 | 检索命中自动上报"使用事件"，供 Decay 强化回弹（PRD-04 消费） | P1 |

## 4. 非功能需求

| ID | 需求 |
|---|---|
| NFR-3.1 | recall p95 延迟 < 300ms（local 模式，10 万级记忆规模） |
| NFR-3.2 | 中英混排 + 代码混杂查询的召回精度不低于纯英文（embedding-gemma 基准） |
| NFR-3.3 | STORAGE_MODE 切换只改 .env，代码零改动 |

## 5. 验收标准

- AC-1：Cursor 中沉淀的偏好，在 Cline 新 session 通过 recall 正确召回（跨客户端继承）；
- AC-2：埋入 1 条关键约束 + 30 条弱相关噪音，recall 返回 ≤5 条且关键约束在内；
- AC-3：两条矛盾偏好共存时，返回结果成对出现且带冲突标注；
- AC-4：全量检索结果 audit 可查 provenance。

## 6. 任务拆分

1. `mcp/tools` —— 四个 MCP 工具定义与协议适配（2d）
2. `core/retrieve/cues` —— 线索提取器（2d）
3. `core/retrieve/hybrid` —— 双路并发 + 重排（3d）
4. `core/retrieve/budget` —— 预算闸与装配器（2d）
5. 双客户端 e2e 联调（1d）

## 7. 依赖

- PRD-01（钢印与 cues schema）
- M0（双库容器）
