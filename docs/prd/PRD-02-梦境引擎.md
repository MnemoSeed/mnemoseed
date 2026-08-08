# PRD-02 · 梦境引擎（Consolidate：快照隔离 + 双轨分流 + De-biasing）

> 对应设计文档：[02-梦境引擎](../design/02-梦境引擎.md)
> 里程碑：M1（本地轨）· 预估 15.5 天（v2 补 LLM 端口与模型配置）

## 1. 目标

实现异步"做梦"巩固：积分池触发 → 只读快照 → 反思提炼 → 双轨分流写回 → 安全清空。全程用户 0 延迟、0 丢字；梦境模型默认走 OAuth / 自带 API key（零硬件门槛），全离线轨（Ollama）为高级可选项。

## 2. 范围

- **In**：触发状态机、快照管理、反思编排（含 De-biasing prompt）、Tier 分流写入、增量 Delta 打桩、失败降级、**LLM 端口与模型路由配置**（FR-2.14）
- **Out**：云端 TEE 部署（PRD-05）、动态模型路由网关（PRD-05）

## 3. 功能需求

| ID | 需求 | 优先级 |
|---|---|---|
| FR-2.1 | 消费积分池触发事件，创建只读快照（turn_range 界定），热层继续接受追加 | P0 |
| FR-2.2 | 反思编排器：完整快照 → 去重折叠 → De-biasing → 三元组提取 → 溯源判定 | P0 |
| FR-2.3 | 双轨分流：Tier 1 提纯直写主基座；Tier 3 锁入隔离图谱；打捞通道（Tier 1 二次反思） | P0 |
| FR-2.4 | 中断保护：做梦/写回期间新对话 0 延迟追加尾部；清空仅覆盖快照范围 | P0 |
| FR-2.5 | Delta 打桩：云端调用时仅发送增量（≤5k tokens），系统指令与既有图谱走 Prompt Cache | P0 |
| FR-2.6 | 降级矩阵：模型调用失败退避重试×3 → 快照落盘；配置的模型端点不可用（OAuth 过期 / API 欠费 / Ollama 离线）时进入"仅捕获"模式 | P1 |
| FR-2.7 | 离线轨（高级可选）：Ollama + ≤14B 量化模型全流程离线跑通；首次设置明示"提炼质量低于云端大模型"警告；70B 级本地模型不作为默认假设 | P1 |
| FR-2.8 | `mnemoseed dream --once` 手动巩固 CLI：M1 阶段先手动触发并人工审查提炼质量，达标后才开自动触发器（先手动再自动纪律） | P0 |
| FR-2.9 | 情绪脱敏：EPISODE 巩固写回后，其分片 emotion 强度按加速 λ 衰减（gist 永存、电荷渐消；overnight therapy，design/02 §10） | P1 |
| FR-2.10 | 图式加速同化：提炼物与既有图谱同构（实体存在+关系模式匹配）走快速固化通道；格格不入者需更多独立证据才放行（Tse 2007，兼作防噪闸门） | P1 |
| FR-2.11 | anima 重染色（re-dye）批处理：换 anima 触发，新核心异步重消化 profile 既有记忆长出新染层/喜好；旧实例染层完整保留（无损切换，design/04 §2.2） | P1 |
| FR-2.12 | 染层/偏好证据边界：更新只消费用户原始输入，永不采纳 agent 渲染输出（防慢漂移自锁，design/02 §5） | P0 |
| FR-2.13 | De-biasing eval harness：染色样本剥除率指标进 CI，剥除率退化即构建失败（单点故障面防线，design/02 §5） | P1 |
| FR-2.14 | **LLM 端口与模型路由配置**：定义 `DreamLLM` Protocol（chat 完成 + 用量统计 + 连通性自检），驱动注册表与存储层同构——驱动：`oauth`（复用订阅：Codex/ChatGPT；MiniMax/Kimi 等中国 CLI 服务商可选，选择时明示数据出境提示）/ `openai_compatible`（Fireworks 等自带 key 端点）/ `anthropic` / `ollama`（高级离线轨，**非默认**）；默认推荐顺序 OAuth > API key > 离线；config.toml 按**角色**分别配置：`deep_reflection`（长背景深睡眠反思）/ `short_increment`（<5k 短增量）/ `local_track` 开关；默认路由按 design/02（深睡眠 → Claude 5 Sonnet，短增量 → GPT-5.6 Terra，本地轨 → Ollama + Llama-3.3-70B）；每角色可独立切换驱动与模型名，改动写审计；连通性自检接口供 console 实测按钮（design/07 §8）调用 | P0 |

## 4. 非功能需求

| ID | 需求 |
|---|---|
| NFR-2.1 | 中断响应延迟 = 0（架构保证，非调优目标） |
| NFR-2.2 | 单次梦境云端计费 ≤ $0.005（5k Delta 上限实测） |
| NFR-2.3 | 快照→写回全程幂等：进程崩溃后重启可从快照落盘恢复，不重复写入 |
| NFR-2.4 | 离线轨（≤14B 量化模型、普通开发机）单次巩固 < 10 分钟 |

## 5. 验收标准

- AC-1：做梦进行到 50% 时插入新对话，新消息完整保留且用户端无卡顿；
- AC-2：同一场景混合 Tier 1/Tier 3 对话 20 轮，做梦后检查：主基座无 Tier 3 来源节点（按 provenance 全量审计）；
- AC-3：同一偏好被提及 10 次，图谱中只有 1 条高置信条目（去重折叠生效）；
- AC-4：由 anima 演绎出强烈口癖的对话做梦后，基座中检索不到任何口癖词（De-biasing 生效；eval harness 剥除率达标）。

## 6. 任务拆分

1. `core/dream/trigger` —— 状态机（2d）
2. `core/dream/snapshot` —— 快照与幂等恢复（2d）
3. `core/dream/reflect` —— 反思编排 + De-biasing prompt 模板（4d）
4. `core/dream/splitter` —— Tier 分流与打捞队列（2d）
5. `core/dream/delta` —— 增量打桩 + Prompt Cache 适配层（2d）
6. `core/llm` —— DreamLLM 端口 + 三驱动 + 角色路由配置（FR-2.14）（1.5d）
7. 集成测试（中断注入、污染审计）（2d）

## 7. 依赖

- PRD-01（积分池事件、钢印 schema）
- 图谱写入层（M0 schema）
