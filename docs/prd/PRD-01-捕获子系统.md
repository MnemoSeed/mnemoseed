# PRD-01 · 捕获子系统（Capture：Local Stripper + 重要性评分 + 积分池）

> 对应设计文档：[01-记忆管线 Stage①](../design/01-记忆管线五阶段.md)
> 里程碑：M1 · 预估 10 天

## 1. 目标

在 MCP 拦截层实现三级过滤漏斗，做到"捕获即拒绝"：90% 体积在本地剥离，只有过闸的持久性信息进入海马体，且每条携带完整元数据钢印。

## 2. 范围

- **In**：mnemoseed-mcp 拦截器、Local Stripper、持久性分类器、三向量评分器、Watermark 积分池
- **Out**：梦境引擎本体（PRD-02）、检索（PRD-03）

## 3. 功能需求

| ID | 需求 | 优先级 |
|---|---|---|
| FR-1.1 | MCP 网关拦截每场对话的每个 Turn，提取 user/assistant 消息与 tool_call 序列 | P0 |
| FR-1.2 | Local Stripper 规则引擎：剥离编译日志、包管理输出、死循环报错堆栈、ANSI 高亮符；规则集可热更新 | P0 |
| FR-1.3 | 持久性分类（三个月测试）：规则（时间限定词/情绪词表）+ 边缘小模型二分类（durable/disposable） | P0 |
| FR-1.4 | 三向量评分 `S = w₁·情绪 + w₂·新颖度 + w₃·因果链`，权重可配置 | P0 |
| FR-1.5 | 积分池：累计 S；`pool ≥ 10.0 且 idle ≥ 5s` 发出梦境触发事件；硬上限 50.0 强制微巩固 | P0 |
| FR-1.6 | 写入钢印：cognitive_tier / model_id / persona_id / cues / provenance / decay_weight=1.0（schema 见设计文档 §1） | P0 |
| FR-1.7 | tool_call 序列结构化存入 `SKILL_SEQUENCE` 原料队列（肌肉记忆管线入口） | P1 |

## 4. 非功能需求

| ID | 需求 |
|---|---|
| NFR-1.1 | 拦截+评分全程 < 50ms，用户无感知 |
| NFR-1.2 | Stripper 压缩率 ≥ 90%（以真实 Claude Code session 日志为基准测试集） |
| NFR-1.3 | 持久性分类在标注集上 precision ≥ 0.9（宁可拒收，不可滥收） |
| NFR-1.4 | 全链路离线可跑（local 模式零外网依赖） |

## 5. 验收标准

- AC-1：灌入一段含 1M tokens 的 Claude Code 原始日志，海马体落盘 ≤ 100k tokens，且人工抽查有效线索无丢失；
- AC-2："这 bug 烦死了"类句子不入库；"我 review 喜欢简洁"类句子入库且 cues 完整；
- AC-3：连续对话累计触发积分池事件，事件 payload 含正确 turn_range。

## 6. 任务拆分

1. `mcp/interceptor` —— MCP 消息钩子与 Turn 切分（2d）
2. `core/stripper` —— 规则引擎 + 规则集 v1（2d）
3. `core/scorer` —— 边缘模型打分服务 + 积分池状态机（3d）
4. `core/schema` —— 钢印 schema + 写入器（2d）
5. 基准测试集与压缩率回归脚本（1d）

## 7. 依赖

- M0 完成（docker-compose 骨架、schema 基座）
- embedding-gemma 容器就绪
