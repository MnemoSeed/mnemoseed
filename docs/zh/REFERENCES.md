# REFERENCES · 理论与文献来源

> 设计文档中引用的所有理论在此备案。
> 核实状态图例：
> **✅** = 2026-08-08 通过 Crossref 数据库逐条核实（作者/年份/期刊命中）
> **📕** = 经典专著（无 DOI，属教科书级常识性引用）
> **⚠️** = 高置信经典文献但本次未在数据库直接命中，标注待抽查（诚实原则：未验证不假装已验证）

---

## 记忆系统架构理论

| # | 引用 | 用于 | 状态 |
|---|---|---|---|
| R1 | McClelland, J. L., McNaughton, B. L., & O'Reilly, R. C. (1995). Why there are complementary learning systems in the hippocampus and neocortex. *Psychological Review*, 102(3), 419–457. DOI: 10.1037/0033-295X.102.3.419 | 双库架构（海马体/皮层分工） | ✅ DOI 直查命中 |
| R2 | Wilson, M. A., & McNaughton, B. L. (1994). Reactivation of hippocampal ensemble memories during sleep. *Science*, 265(5172), 676–679. | 梦境引擎（睡眠期回放巩固） | ✅ |
| R3 | Frey, U., & Morris, R. G. M. (1997). Synaptic tagging and long-term potentiation. *Nature*, 385, 533–536. | Capture 选择性编码闸门 | ✅ |
| R4 | Tulving, E., & Thomson, D. M. (1973). Encoding specificity and retrieval processes in episodic memory. *Psychological Review*, 80(5), 352–373. | cues 元数据 / 情境化检索 | ✅ |
| R5 | Nader, K., Schafe, G. E., & LeDoux, J. E. (2000). Fear memories require protein synthesis in the amygdala for reconsolidation after retrieval. *Nature*, 406, 722–726. | Reconcile 再巩固改写协议 | ✅ |
| R6 | Tononi, G., & Cirelli, C. (2003). Sleep and synaptic homeostasis: A hypothesis. *Brain Research Bulletin*, 62(2), 143–150.（扩展版：2014, *Neuron*） | Decay 衰减引擎 / 深度睡眠清扫 | ✅ |
| R7 | Johnson, M. K., Hashtroudi, S., & Lindsay, D. S. (1993). Source monitoring. *Psychological Bulletin*, 114(1), 3–28. | Provenance 溯源主线 | ✅ |
| R8 | Ebbinghaus, H. (1885/1913). *Memory: A Contribution to Experimental Psychology*. | Decay 遗忘曲线 | 📕 |
| R9 | Cepeda, N. J., Pashler, H., Vul, E., Wixted, J. T., & Rohrer, D. (2006). Distributed practice in verbal recall tasks: A review and quantitative synthesis. *Psychological Bulletin*, 132(3), 354–380. | 强化回弹（spacing effect） | ✅ |
| R10 | Hebb, D. O. (1949). *The Organization of Behavior*. Wiley. | 捕获时近重复强化 / 共现边 | 📕 |
| R11 | Collins, A. M., & Loftus, E. F. (1975). A spreading-activation theory of semantic processing. *Psychological Review*, 82(6), 407–428. DOI: 10.1037/0033-295X.82.6.407 | 共现边扩散激活检索 | ✅ DOI 直查命中 |
| R12 | Godden, D. R., & Baddeley, A. D. (1975). Context-dependent memory in two natural environments: On land and underwater. *British Journal of Psychology*, 66(3), 325–331. | Reconcile 情境作用域共存分支 | ✅ |
| R13 | Brainerd, C. J., & Reyna, V. F. (1990). Gist is the grist: Fuzzy-trace theory and the new intuitionism. *Developmental Review*, 10(1), 3–47. DOI: 10.1016/0273-2297(90)90003-M | Verbatim/Gist 双通道（双库理论命名） | ✅ Crossref 命中 |
| R14 | Tolman, E. C. (1948). Cognitive maps in rats and men. *Psychological Review*, 55(4), 189–208. + O'Keefe, J., & Nadel, L. (1978). *The Hippocampus as a Cognitive Map*. | 空间隐喻的拒绝依据（design/05） | ✅(Tolman) / 📕(O'Keefe & Nadel) |
| R15 | Miller, G. A. (1956). The magical number seven, plus or minus two. *Psychological Review*, 63(2), 81–97. DOI: 10.1037/h0043158 + Cowan, N. (2001). The magical number 4 in short-term memory. *Behavioral and Brain Sciences*, 24(1), 87–114. | 抗稀释 top-k 上限 | ✅ 两篇均 DOI 直查命中 |

## 情绪与记忆（design/01 §1.6 专用）

| # | 引用 | 用于 | 状态 |
|---|---|---|---|
| R16 | McGaugh, J. L. (2000). Memory—A century of consolidation. *Science*, 287(5451), 248–251. | 情绪调制巩固强度（arousal 进评分） | ✅ |
| R17 | Kensinger, E. A., & Corkin, S. (2003). Memory enhancement for emotional words: Are emotional words more vividly remembered than neutral words? *Memory & Cognition*, 31, 1169–1180. | arousal 主轴 / valence 降级为 cue | ✅ |
| R18 | Brown, R., & Kulik, J. (1977). Flashbulb memories. *Cognition*, 5(1), 73–99. | 闪光灯记忆概念 | ✅ |
| R19 | Neisser, U., & Harsch, N. (1992). Phantom flashbulbs: False recollections of hearing the news about Challenger. 收录于 Winograd & Neisser 编 *Affect and Accuracy in Recall*. | 闪光灯悖论：情绪分不得进 confidence | ✅ |
| R20 | Yerkes, R. M., & Dodson, J. D. (1908). The relation of strength of stimulus to rapidity of habit-formation. *Journal of Comparative Neurology and Psychology*, 18(5), 459–482. | arousal 饱和截顶（倒 U） | ✅ |
| R21 | Easterbrook, J. A. (1959). The effect of emotion on cue utilization and the organization of behavior. *Psychological Review*, 66(3), 183–201. | 高唤醒周边信息缺口标记 | ✅ |
| R22 | Christianson, S.-Å. (1992). Emotional stress and eyewitness memory: A critical review. *Psychological Bulletin*, 112(2), 284–309. | 武器聚焦（中心强/周边弱） | ✅ |
| R23 | Russell, J. A. (1980). A circumplex model of affect. *Journal of Personality and Social Psychology*, 39(6), 1161–1178. DOI: 10.1037/h0077714 | V/A 二维情绪模型 | ✅ DOI 解析命中（APA 站点） |
| R24 | Bradley, M. M., & Lang, P. J. (1994). Measuring emotion: The self-assessment manikin and the semantic differential. *Journal of Behavior Therapy and Experimental Psychiatry*, 25(1), 49–59. | SAM 九点量表（标注基准） | ✅ |
| R25 | Watson, D., Clark, L. A., & Tellegen, A. (1988). Development and validation of brief measures of positive and negative affect: The PANAS scales. *Journal of Personality and Social Psychology*, 54(6), 1063–1070. | 效价测量工具 | ✅ |
| R26 | Bower, G. H. (1981). Mood and memory. *American Psychologist*, 36(2), 129–148. DOI: 10.1037/0003-066X.36.2.129 | 心境一致性检索加权 | ✅ DOI 直查命中 |
| R27 | Mohammad, S. M. (2018). Obtaining reliable human ratings of valence, arousal, and dominance for 20,000 English words. *Proceedings of ACL 2018*. | 文本情绪自动量化词表 | ✅ |

## 业界文献（非学术，一手来源已读全文核实）

| # | 来源 | 获取方式 |
|---|---|---|
| I1 | wast3《Memory Engineering: The Discipline That Decides Whether Your AI Agent Has a Past》X, 2026-08-04 | ✅ 2026-08-08 浏览器直接读全文 |
| I2 | N01ennn《How to be a Memory Engineer, from the perspective of Stanford, Microsoft, Anthropic and Nvidia》X, 2026-08-03 | ✅ 2026-08-08 浏览器直接读全文。原始出处追溯结果见 I2a–I2d |
| I2a | Stanford: *Agent Memory: Characterization and System Implications of Stateful Long-Horizon Workloads*, arXiv:2606.06448（2026-06-04 提交） | ✅ 一手核实：摘要确认四家族分类法、写/读路径成本归因、系统级 profiling harness，与转述相符 |
| I2b | Microsoft: *PlugMem: A Task-Agnostic Plugin Memory Module for LLM Agents*, arXiv:2603.03296（2026-02-06 提交） | ✅ 一手核实：摘要确认"facts/skills 不存 logs""信息密度"指标、跨三基准超过专用设计，与转述相符 |
| I2c | Microsoft "Memento"（自称 fine-tune 让模型自写 dense note、删原始推理、峰值内存降 2–3x、重建损失 15 分） | ❌ **未能在 arXiv 定位同名原始论文**（37 篇同名论文逐一排查均不符）。具体数字降级为"未核实转述"，设计中不依赖 |
| I2d | Anthropic "Built-in Memory for Claude Managed Agents"（97% 首遍错误率降幅等） | ⚠️ 属产品文档/博客性质，非学术论文；数字未独立核实，营销引用时不得标注为研究结论 |
| I3 | Claude-Mem (github.com/thedotmack/claude-mem) README | ✅ 2026-08-08 直接读取 |
| I4 | MemPalace（本地部署的记忆系统） | ✅ 第一手日常使用观察 |
| I5 | Mem0 官网定价页 mem0.ai/pricing（四档价格、Dream/Graph memory 付费墙） | ✅ 2026-08-08 浏览器直接读取 |
| I6 | Zep 官网 getzep.com（双时态模型、LoCoMo/LongMemEval 基准、治理与部署选项） | ✅ 2026-08-08 浏览器直接读取 |
| I7 | dev.to《Mem0 vs Zep vs LangMem vs MemoClaw: AI Agent Memory Comparison 2026》 | ⚠️ 第三方文章且作者为 MemoClaw 官方（文中已披露立场）；其优缺点描述用于交叉印证，定价以官网为准 |
| I8 | Evermind 博客 evermind.ai（EverOS 四层架构、Memory Perception Modules、benchmark 宣称） | ✅ 2026-08-08 直接读取；其 benchmark 数字为厂商自述未独立复现 |
| I9 | Letta 定价文档 docs.letta.com/pricing（Free/Pro $20/Teams/Developer） | ✅ 2026-08-08 浏览器直接读取 |
| I10 | Cognee 官网 cognee.ai 首页+定价页（token 统一费率、$5/workspace、案例） | ✅ 2026-08-08 浏览器直接读取 |
| I11 | Hindsight 官方文档 hindsight.vectorize.io（retain/recall/reflect、Observations、TEMPR、Memory Bank 配置） | ✅ 2026-08-08 浏览器直接读取 |
| I12 | Memvid 官网 memvid.com（单文件 .mv2、WAL、混合检索 sub-5ms；无公开价格页） | ✅ 2026-08-08 浏览器直接读取 |
| I13 | MemoryLake 官网 memorylake.ai（Memory Passport 六类记忆、Git 式版本化、三权利叙事、31 个 vs 对比页清单） | ✅ 2026-08-08 浏览器直接读取；其 LoCoMo "Global #1" 为厂商自述未独立复现 |

## 情绪与记忆补充（竞品调研新增）

| # | 引用 | 用于 | 状态 |
|---|---|---|---|
| R28 | Craik, F. I. M., & Lockhart, R. S. (1972). Levels of processing: A framework for memory research. *Journal of Verbal Learning and Verbal Behavior*, 11(6), 671–684. DOI: 10.1016/S0022-5371(72)80001-X | importance_hint 用户显式加权的依据（意向性编码优先于偶然编码） | ✅ Crossref 命中 |

## 人格、偏好与系统动力学（2026-08-08 anima 模型 + 系统复盘新增）

| # | 引用 | 用于 | 状态 |
|---|---|---|---|
| R29 | Fleeson, W. (2001). Toward a structure- and process-integrated view of personality. *Journal of Personality and Social Psychology*, 80(6), 1011–1027. | 特质=密度分布（mean+width 量化骨架）；说话方式是性格的自然流露 | ✅ Crossref 命中（2026-08-11 抽查，DOI 10.1037/0022-3514.80.6.1011） |
| R30 | McAdams, D. P., & Pals, J. L. (2006). A new Big Five. *American Psychologist*, 61(3), 204–217. | anima 三层架构（先天特质/特征适应/叙事身份） | ✅ Crossref 命中（2026-08-11 抽查，DOI 10.1037/0003-066x.61.3.204） |
| R31 | Cloninger, C. R., Svrakic, D. M., & Przybeck, T. R. (1993). A psychobiological model of temperament and character. *Archives of General Psychiatry*, 50(12), 975–990. | 核心 immutable vs 染层可塑（temperament/character 分离） | ✅ Crossref 命中（2026-08-11 抽查，DOI 10.1001/archpsyc.1993.01820240059008） |
| R32 | Markus, H., & Wurf, E. (1987). The dynamic self-concept. *Annual Review of Psychology*, 38, 299–337. | 多自我按情境激活（anima 可切换的依据） | ✅ Crossref 命中（2026-08-11 抽查，DOI 10.1146/annurev.ps.38.020187.001503） |
| R33 | Hong, Y., Morris, M. W., Chiu, C., & Benet-Martínez, V. (2000). Multicultural minds. *American Psychologist*, 55(7), 709–720. | frame switching（换 anima = 切换整套行为倾向） | ✅ Crossref 命中（2026-08-11 抽查，DOI 10.1037/0003-066x.55.7.709） |
| R34 | Bem, D. J. (1972). Self-perception theory. *Advances in Experimental Social Psychology*, 6, 1–62. | 行为证据 > 陈述证据（偏好更新权重） | ✅ Crossref 命中（2026-08-11 抽查，DOI 10.1016/s0065-2601(08)60024-6） |
| R35 | De Houwer, J. (2007). A conceptual and theoretical analysis of evaluative conditioning. *The Spanish Journal of Psychology*, 10(2), 230–241. | 情绪共现染色偏好 | ✅ Crossref 命中（2026-08-11 抽查，DOI 10.1017/s1138741600006491） |
| R36 | Zajonc, R. B. (1968). Attitudinal effects of mere exposure. *Journal of Personality and Social Psychology*, 9(2), 1–27. | 曝光计数喂偏好更新（低权重、有饱和） | ✅ Crossref 命中（2026-08-11 抽查，DOI 10.1037/h0025848） |
| R37 | Schultz, W., Dayan, P., & Montague, P. R. (1997). A neural substrate of prediction and reward. *Science*, 275(5306), 1593–1599. | 奖赏预测误差驱动价值更新（行为证据主通路） | ✅ Crossref 命中（2026-08-11 抽查，DOI 10.1126/science.275.5306.1593） |
| R38 | Levy, D. J., & Glimcher, P. W. (2011). Comparing apples and oranges. *Annals of the New York Academy of Sciences*, 1239, 12–24. | vmPFC 共同价值货币 | ✅ 作者/标题经 J Neurosci 姊妹版核实（DOI 10.1523/jneurosci.2218-11.2011）；Annals 版未在 Crossref 单独命中 |
| R39 | Roberts, B. W., Wood, D., & Smith, J. L. (2005). Evaluating five factor theory and social investment perspectives on personality trait development. *Journal of Research in Personality*, 39(1), 166–184. | 人格定向漂移（人生阶段事件触发重估）；特质终身可变 | ✅ Crossref 命中（2026-08-11 抽查，DOI 10.1016/j.jrp.2004.08.002） |
| R40 | Anderson, M. C., Bjork, R. A., & Bjork, E. L. (1994). Remembering can cause forgetting. *Journal of Experimental Psychology: Learning, Memory, and Cognition*, 20(5), 1063–1087. | 检索-衰减正反馈环（retrieval-induced forgetting）→ 多样性/探索配额 | ✅ Crossref 命中（2026-08-11 抽查，DOI 10.1037/0278-7393.20.5.1063） |
| R41 | Wixted, J. T. (2004). The psychology and neuroscience of forgetting. *Annual Review of Psychology*, 55, 235–269. | 干扰项衰减（遗忘主引擎是干扰不是时间） | ✅ Crossref 命中（2026-08-11 抽查，DOI 10.1146/annurev.psych.55.090902.141555） |
| R42 | Walker, M. P., & van der Helm, E. (2009). Overnight therapy? *Psychological Bulletin*, 135(5), 731–748. | 梦境情绪脱敏（gist 永存、电荷渐消） | ✅ Crossref 命中（2026-08-11 抽查，DOI 10.1037/a0016570） |
| R43 | Tse, D., et al. (2007). Schemas and memory consolidation. *Science*, 316(5821), 76–82. | 图式加速同化（同构快速通道/异构加证据） | ✅ Crossref 命中（2026-08-11 抽查，DOI 10.1126/science.1135935） |
| R44 | Forer, B. R. (1949). The fallacy of personal validation. *Journal of Abnormal and Social Psychology*, 44(1), 118–123. | 六边形可视化必须显示不确定性（防 Barnum 伪造精确） | ✅ Crossref 命中（2026-08-11 抽查，DOI 10.1037/h0059240） |
| R45 | MacLeod, C., Mathews, A., & Tata, P. (1986). Attentional bias in emotional disorders. *Journal of Abnormal Psychology*, 95(1), 15–20. | 捕获中性红线（anima 不参与捕获评分） | ✅ Crossref 命中（2026-08-11 抽查，DOI 10.1037/0021-843x.95.1.15） |
| R46 | Pearce, J. M., & Hall, G. (1980). A model for Pavlovian learning. *Psychological Review*, 87(6), 532–552. | 学习率 ∝ 不确定性（偏好 Kalman 式更新） | ✅ Crossref 命中（2026-08-11 抽查，DOI 10.1037/0033-295x.87.6.532） |
| R47 | Quoidbach, J., Gilbert, D. T., & Wilson, T. D. (2013). The end of history illusion. *Science*, 339(6115), 96–98. | 偏好漂移叙事（人低估自己未来的变化） | ✅ Crossref 命中（2026-08-11 抽查，DOI 10.1126/science.1229294） |

## 梦境预算与系统动力学（动态 Delta 预算新增）

| # | 引用 | 用于 | 状态 |
|---|---|---|---|
| R48 | Borbély, A. A. (1982). A two process model of sleep regulation. *Human Neurobiology*, 1(3), 195–204. | 动态预算动机：积分池 = Process S 睡眠压力，梦长随睡眠债伸缩而非固定 | ⚠️ 高置信经典；原刊 Human Neurobiology 未被 Crossref 收录（已停刊），PubMed PMID 7185792 可查 |
| R49 | Little, J. D. C. (1961). A proof for the queuing formula: L = λW. *Operations Research*, 9(3), 383–387. DOI: 10.1287/opre.9.3.383 | 稳态校验：长期到达率 ≤ 清算能力，否则任何有限预算都积压 | ✅ Crossref 命中（DOI 验证 2026-08-10） |
| R50 | Dement, W. (1960). The effect of dream deprivation. *Science*, 131(3415), 1705–1707. | REM rebound：剥夺后超量补偿——积压期预算扩张的生理学对应 | ✅ Crossref 命中（2026-08-11 抽查，DOI 10.1126/science.131.3415.1705） |

---

*维护规则：任何文档新增理论引用时必须同步登记本表并标注核实状态；标注 ⚠️ 的条目在后续迭代中逐条抽查转正或替换。*
