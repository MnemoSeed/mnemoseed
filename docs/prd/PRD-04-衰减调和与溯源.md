# PRD-04 · 衰减、调和与溯源（Decay + Reconcile + Provenance）

> 对应设计文档：[01-记忆管线 Stage④⑤ⓟ](../design/01-记忆管线五阶段.md)
> 里程碑：M2 · 预估 14 天 —— **差异化核心，记忆质量的护城河**

## 1. 目标

让记忆库"用一年仍可信"：未强化记忆自然沉底、事实变更正确接管、冲突显性化、每条记忆可审计。

## 2. 范围

- **In**：衰减引擎（权重计算/分层 λ/软归档）、强化回弹、Reconcile 双子协议（写入检测 + 提取侧再巩固）、历史版本链、审计接口
- **Out**：衰减参数的领域化自动调优（v4.x 后续）

## 3. 功能需求

| ID | 需求 | 优先级 |
|---|---|---|
| FR-4.1 | 衰减计算：`w = base_confidence × exp(-λ_eff × days)`，`λ_eff = λ_base × (1 + κ × interference_load)`——相似邻居越多衰减越快（干扰理论 Wixted 2004，独特记忆天然抗衰）；λ_base 按记忆类型分层（fact 0.01 / preference 0.005 / episode 0.03） | P0 |
| FR-4.2 | 强化回弹：检索使用事件触发 `last_reinforced=now`、`w` 回弹；**间隔效应冷却**——短时间窗内重复召回收益递减（Cepeda 2006），防集中刷权重 | P0 |
| FR-4.3 | 软衰落阶梯：w<0.4 沉底（不进 top-k）→ w<0.1 冻结（不检索）→ w<0.05 且 90 天无访问归档（移出索引）；显式查询可复活（w→0.5） | P0 |
| FR-4.4 | 永不衰减白名单：provenance、用户 pin、compliance/safety 约束 | P0 |
| FR-4.5 | 写入侧冲突检测：同主同谓比对 → 相同强化 / **cues 可划界则情境作用域共存** / 可裁决则 invalidate 接管 / 不可裁决 flag_conflict（四分支，情境共存优先于裁决） | P0 |
| FR-4.5b | 冲突确认渲染接口：引擎只输出结构化冲突对象（old/new + provenance），措辞由在任 anima 演绎（性格核心+染层 → 语气），引擎不得自带话术（anima 模型见 design/04 §2） | P0 |
| FR-4.5c | **偏好调和分支**：PREFERENCE 节点不走矛盾四分支——新旧偏好按漂移语义共存于版本链（"当时生效的我"），更新规则 `Δvalence = 学习率(∝prior_width) × 证据强度 × 类型权重(行为>陈述>情绪共现>曝光)`；证据只取自用户原始输入（design/01 §7、02 §5） | P0 |
| FR-4.1b | 动态 λ 自校准：按沉底记忆的复活率反馈调节各层 λ（预留接口，初值手工） | P2 |
| FR-4.3b | 源失效降权：provenance.source 失效的记忆自动额外降权（MemPalace sync 模式） | P1 |
| FR-4.2b | 捕获时赫布强化：近重复命中即回弹，不等做梦（与 PRD-01 写入前 dedup 检查联动） | P0 |
| FR-4.6 | 提取侧再巩固：检索命中开 labile 窗口，新矛盾事实改写旧槽位，旧版本入历史链（valid_to），绝不物理删除 | P0 |
| FR-4.7 | 裁决判据：时间戳明确性 + 来源权威度差（user 显式 > Tier1 推断 > Tier3 推断） | P0 |
| FR-4.8 | 审计接口：任意记忆返回完整 provenance.history（创建/改写链） | P0 |
| FR-4.9 | 冲突上浮：flag_conflict 达阈值或涉及高严重度约束时，主动向用户发起二选一确认 | P1 |

## 4. 非功能需求

| ID | 需求 |
|---|---|
| NFR-4.1 | 衰减批处理每日一次，10 万级记忆全量重算 < 60s |
| NFR-4.2 | 历史版本链查询（timeline）p95 < 500ms |
| NFR-4.3 | 任何改写操作幂等且可追溯（重演 history 可重建任意时点状态） |

## 5. 验收标准

- AC-1：模拟时间快进 60 天，未访问记忆 w 正确衰减、被访问记忆保持高位；
- AC-2：用户先说"我用 Neovim"，30 天后说"我换回 VSCode 了"——recall 只返回 VSCode，timeline 可查到完整变更链；
- AC-3：低置信来源试图覆盖高置信事实时，不覆盖而是生成 flag_conflict；
- AC-4：构造 5 万条记忆运行 90 天模拟，recall 的 top-5 命中率相对第 1 天下降 < 10%（抗垃圾填埋场）。

## 6. 任务拆分

1. `core/decay/engine` —— 权重计算 + 分层 λ + 批处理（3d）
2. `core/decay/reinforce` —— 使用事件消费与回弹（1d）
3. `core/reconcile/detector` —— 写入侧冲突检测与裁决（3d）
4. `core/reconcile/reconsolidate` —— labile 窗口与版本链（3d）
5. `core/audit` —— provenance 查询与 timeline API（2d）
6. 时间快进模拟测试框架（2d）

## 7. 依赖

- PRD-02（写回路径挂检测钩子）
- PRD-03（检索侧使用事件、conflict 成对返回）
