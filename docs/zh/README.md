# MnemoSeed 设计文档索引（中文工作稿）

> 本目录是**中文工作树**：日常设计/PRD 更新先落在这里。英文正典在 `docs/design/`、`docs/prd/`，每次 merge 到 main / 发版前从本树同步。
> 所有图表使用 Mermaid.js 记录与渲染。

## 目录

### 设计文档（design/）

| 文档 | 内容 |
|---|---|
| [00-总览与设计哲学](design/00-总览与设计哲学.md) | 定位、脑神经科学基础映射表、总体架构图 |
| [01-记忆管线五阶段](design/01-记忆管线五阶段.md) | Capture / Consolidate / Retrieve / Reconcile / Decay + Provenance 全管线设计 |
| [02-梦境引擎](design/02-梦境引擎.md) | 触发状态机、快照隔离、中断保护、双轨分流写入、增量脱水 |
| [03-存储与检索](design/03-存储与检索.md) | 混合双库总线、STORAGE_MODE 路由矩阵、混合检索、抗稀释策略 |
| [04-隔离解耦与隐私](design/04-隔离解耦与隐私.md) | 认知分级隔离、BYOK/TEE 零知识架构 |
| [09-anima与偏好动力学](design/09-anima与偏好动力学.md) | **进阶模块（不在 M1 首发）**：Anima 灵魂模型（三层一体/无损切换）、偏好贝叶斯后验更新、特质雷达可视化 |
| [05-业界对标与精华提取](design/05-业界对标与精华提取.md) | Stanford/Microsoft/Anthropic/Nvidia 四透镜、Claude-Mem、MemPalace 拆解与取舍决策记录 |
| [06-接入与安装体验](design/06-接入与安装体验.md) | 三层适配架构（daemon/MCP/plugin）、profile 凭证身份模型（login/link）、宿主能力矩阵、3 分钟安装流程、断开卸载语义 |
| [07-管理控制台](design/07-管理控制台.md) | MnemoSeed Console：profiles 管理、记忆浏览、图谱可视化、单条记忆全档案、梦境面板与 token 用量、模型路由设置 |
| [08-多端同步与冲突合并](design/08-多端同步与冲突合并.md) | CRDT/CALM/HLC 理论基座：四类数据的合并机制盘点、反熵同步协议、巩固租约、Tombstone 删除 |

### 开发任务 PRD（prd/）

| 文档 | 模块 |
|---|---|
| [PRD-00 路线图与里程碑](prd/PRD-00-路线图.md) | M0–M4 里程碑、依赖关系、优先级 |
| [PRD-01 捕获子系统](prd/PRD-01-捕获子系统.md) | Local Stripper、重要性评分、Watermark 积分池 |
| [PRD-02 梦境引擎](prd/PRD-02-梦境引擎.md) | 异步巩固、快照、Tier 分流、De-biasing |
| [PRD-03 检索与 MCP 网关](prd/PRD-03-检索与MCP网关.md) | 混合检索 API、MCP 工具定义、上下文装配 |
| [PRD-04 衰减调和与溯源](prd/PRD-04-衰减调和与溯源.md) | Decay 权重、Reconcile 冲突协议、Provenance schema |
| [PRD-05 云端同步与 TEE](prd/PRD-05-云端同步与TEE.md) | E2EE 同步、Nitro Enclaves、计费套利网关 |
| [PRD-06 宿主接入与安装](prd/PRD-06-宿主接入与安装.md) | daemon embedded 模式、installer、login/link 身份绑定、Claude Code plugin（hooks）、MCP 降级模式、uninstall |
| [PRD-07 管理控制台](prd/PRD-07-管理控制台.md) | Console SPA：Dashboard/Profiles/记忆浏览/梦境审查/冲突收件箱（M1 只读核心，M2 补全） |
| [PRD-08 M0 地基](prd/PRD-08-M0地基.md) | repo 骨架 + CI、四存储接口双驱动、capability 校验、Schema v1 冻结（阻塞全部后续 PRD） |

## 理论文献备案

**[REFERENCES.md](REFERENCES.md)** —— 所有引用理论的完整出处与核实状态（✅ Crossref 已核实 / 📕 经典专著 / ⚠️ 待抽查）。铁律：未验证的信息必须标注，不允许靠推测或记忆充数。

## 外部理论来源

1. **wast3《Memory Engineering》**（X, 2026-08-04, 182.8K views）—— 五阶段记忆管线框架（Capture/Consolidate/Retrieve/Reconcile/Decay）及评论区 Provenance 补充。本文档不是照抄，而是将其映射到互补学习系统（CLS）、再巩固（Reconsolidation）、突触稳态（SHY）等脑神经科学机制上重新推导。
2. **N01ennn《How to be a Memory Engineer》**（X, 2026-08-03）—— Stanford/Microsoft/Anthropic/Nvidia 四透镜，15 步工程纪律。拆解与取舍见 design/05。
3. **Claude-Mem / MemPalace** —— 两个实际运行中的记忆系统的概念拆解与取舍（design/05）。
4. **MnemoSeed 白皮书 v3.1 / PRD v3.0 / 创世白皮书**（前期讨论产物）—— 双库架构、梦境引擎、认知分级、脱水节流阀、定价与 PLG 战略。
