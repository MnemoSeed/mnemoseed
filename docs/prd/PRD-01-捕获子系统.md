# PRD-01 · 捕获子系统（Capture：Local Stripper + 重要性评分 + 积分池）

> 对应设计文档：[01-记忆管线 Stage①](../design/01-记忆管线五阶段.md)
> 里程碑：M1 · 预估 10 天

## 1. 目标

在 daemon 捕获端实现三级过滤漏斗，做到"捕获即拒绝"：90% 体积在本地剥离，只有过闸的持久性信息进入海马体，且每条携带完整元数据钢印。

## 2. 范围

- **In**：daemon `/ingest` 捕获端点（Tier 1 宿主 hook 直推，零 token 不经 MCP；宿主侧 hook 脚本属 PRD-06）、Local Stripper、持久性分类器、三向量评分器、Watermark 积分池、`memory.remember` 显式通路的 ingest 支持
- **Out**：梦境引擎本体（PRD-02）、检索（PRD-03）、宿主 hook 脚本与 MCP server 拦截器（PRD-06 / mcp repo）、SKILL_SEQUENCE 原料队列（推迟至 M2，随肌肉记忆管线消费方一起设计）

## 3. 功能需求

| ID | 需求 | 优先级 |
|---|---|---|
| FR-1.1 | daemon `/ingest` 端点接收宿主 hook 直推的每场对话每个 Turn（2s 超时 fail-open 由调用侧保证），提取 user/assistant 消息与 tool_call 序列，完成 Turn 切分与结构化 | P0 |
| FR-1.2 | Local Stripper 规则引擎：剥离编译日志、包管理输出、死循环报错堆栈、ANSI 高亮符；规则集可热更新 | P0 |
| FR-1.3 | 持久性分类（三个月测试）v1：规则（时间限定词/情绪词表）+ 词表与嵌入启发式（durable/disposable）；**v1 不引入边缘小模型**——先在基准标注集上实测 precision，未达 NFR-1.3 再以小模型补强（选型与校准届时以标注集为据） | P0 |
| FR-1.4 | 评分 `S = w₁·min(arousal,θ_cap) + w₂·新颖度 + w₃·因果链`（arousal 截顶、valence 仅存 cues、emotion 永不进 confidence——依据见 design/01 §1.6），权重可配置。v1 量化手段：arousal/valence 走手工策划种子词表（NRC VAD 形态，EN+ZH 各数百条；NRC VAD 本体为申请表门控资源不可自动分发，后续以校准资源替换）；新颖度 = bge-m3 嵌入与近期分片的距离；因果链 = 规则特征（连接词/决策句式） | P0 |
| FR-1.5 | 积分池：累计 S；`pool ≥ 10.0 且 idle ≥ 5s` 发出梦境触发事件；硬上限 50.0 强制微巩固 | P0 |
| FR-1.6 | 写入钢印：cognitive_tier / model_id / anima_id（当时在任灵魂）/ cues（**含 entities 字段**，Freshness Guard 检索侧过滤依赖；**含 host / task 编码情境字段**，编码特异性检索侧依赖——可空不可缺席，schema 在 M0 冻结前预留）/ provenance / decay_weight=1.0（schema 见设计文档 §1） | P0 |
| FR-1.6b | **捕获中性红线**：F1–F3 评分与过滤全程禁止读取 anima 状态与 PREFERENCE 节点（anima 只染检索与渲染，捕获必须中性——design/01 §1 红线；CI 加静态检查防回归） | P0 |
| FR-1.7 | ~~tool_call 序列结构化存入 `SKILL_SEQUENCE` 原料队列~~ **推迟至 M2**（队列表随肌肉记忆管线消费方一起设计，避免先建表再改 schema） | P1→M2 |
| FR-1.8 | 近重复检查双分支：≥0.9 且一致 → Hebbian `last_reinforced` 回弹不产生新分片；≥0.85 但极性/取值/时间冲突（规则+轻量分类器，零额外 LLM 调用）→ 命中图谱节点置 `needs_reconcile=true` 且积分池 +2.0（预测误差加速巩固，design/02 §9.1） | P0 |
| FR-1.9 | `memory.remember` 支持调用方显式传 `importance_hint`（0–1），与自动 S 评分取 max——用户说"记住这个"时显式意图盖过算法判断（意向性编码，R28 Craik & Lockhart 1972） | P1 |

## 4. 非功能需求

| ID | 需求 |
|---|---|
| NFR-1.1 | 拦截+评分全程 < 50ms，用户无感知 |
| NFR-1.2 | **可剥离噪音剥离率 ≥ 90%**（基准：真实 Claude Code session 日志；分子分母都只计规则命中的噪音类内容——编译日志/包管理输出/进度条/ANSI 等）。全量字节压缩率降为观察指标（强依赖输入内容群体：编码重型日志 ≈90%+，研究/设计型对话实测仅 0.4%，不是缺陷），回归脚本两者都报 |
| NFR-1.3 | 持久性分类在标注集上 precision ≥ 0.9（宁可拒收，不可滥收） |
| NFR-1.4 | 全链路离线可跑（local 模式零外网依赖） |

## 5. 验收标准

- AC-1：灌入一段含 1M tokens 的 Claude Code 原始日志，海马体落盘 ≤ 100k tokens，且人工抽查有效线索无丢失；
- AC-2："这 bug 烦死了"类句子不入库；"我 review 喜欢简洁"类句子入库且 cues 完整；
- AC-3：连续对话累计触发积分池事件，事件 payload 含正确 turn_range。

## 6. 任务拆分

1. `daemon/ingest` —— `/ingest` 端点 + Turn 切分与结构化（user/assistant/tool_call 序列）（2d）
2. `core/stripper` —— 规则引擎 + 规则集 v1（2d）
3. `core/scorer` —— 词表/嵌入评分器 + 积分池状态机（3d）
4. `core/capture` —— 钢印组装 + 写入器（近重复双分支、needs_reconcile 置位、Hebbian 回弹）（2d）
5. 基准测试集与压缩率回归脚本（真实 Claude Code session 日志为基准；持久性标注集 v1）（1d）

> 每任务 = 一次 programmer 派遣 + verifier 验收，TDD：先按 FR/AC 写失败测试再实现（协作流程见 `.claude/agents/`）。

## 7. 依赖

- M0 完成（docker-compose 骨架、schema 基座）
- bge-m3 ONNX 嵌入就绪
