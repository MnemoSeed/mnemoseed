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
| R1 | McClelland, J. L., McNaughton, B. L., & O'Reilly, R. C. (1995). Why there are complementary learning systems in the hippocampus and neocortex. *Psychological Review*, 102(3), 419–457. | 双库架构（海马体/皮层分工） | ⚠️ Crossref 返回其 2002 年再版书章，1995 原版为学界公认，待抽查 |
| R2 | Wilson, M. A., & McNaughton, B. L. (1994). Reactivation of hippocampal ensemble memories during sleep. *Science*, 265(5172), 676–679. | 梦境引擎（睡眠期回放巩固） | ✅ |
| R3 | Frey, U., & Morris, R. G. M. (1997). Synaptic tagging and long-term potentiation. *Nature*, 385, 533–536. | Capture 选择性编码闸门 | ✅ |
| R4 | Tulving, E., & Thomson, D. M. (1973). Encoding specificity and retrieval processes in episodic memory. *Psychological Review*, 80(5), 352–373. | cues 元数据 / 情境化检索 | ✅ |
| R5 | Nader, K., Schafe, G. E., & LeDoux, J. E. (2000). Fear memories require protein synthesis in the amygdala for reconsolidation after retrieval. *Nature*, 406, 722–726. | Reconcile 再巩固改写协议 | ✅ |
| R6 | Tononi, G., & Cirelli, C. (2003). Sleep and synaptic homeostasis: A hypothesis. *Brain Research Bulletin*, 62(2), 143–150.（扩展版：2014, *Neuron*） | Decay 衰减引擎 / 深度睡眠清扫 | ✅ |
| R7 | Johnson, M. K., Hashtroudi, S., & Lindsay, D. S. (1993). Source monitoring. *Psychological Bulletin*, 114(1), 3–28. | Provenance 溯源主线 | ✅ |
| R8 | Ebbinghaus, H. (1885/1913). *Memory: A Contribution to Experimental Psychology*. | Decay 遗忘曲线 | 📕 |
| R9 | Cepeda, N. J., Pashler, H., Vul, E., Wixted, J. T., & Rohrer, D. (2006). Distributed practice in verbal recall tasks: A review and quantitative synthesis. *Psychological Bulletin*, 132(3), 354–380. | 强化回弹（spacing effect） | ✅ |
| R10 | Hebb, D. O. (1949). *The Organization of Behavior*. Wiley. | 捕获时近重复强化 / 共现边 | 📕 |
| R11 | Collins, A. M., & Loftus, E. F. (1975). A spreading-activation theory of semantic processing. *Psychological Review*, 82(6), 407–428. | 共现边扩散激活检索 | ⚠️ Crossref 仅命中 1988 重印版，1975 原版待抽查 |
| R12 | Godden, D. R., & Baddeley, A. D. (1975). Context-dependent memory in two natural environments: On land and underwater. *British Journal of Psychology*, 66(3), 325–331. | Reconcile 情境作用域共存分支 | ✅ |
| R13 | Brainerd, C. J., & Reyna, V. F. (1990 起). Fuzzy-Trace Theory 系列. | Verbatim/Gist 双通道（双库理论命名） | ⚠️ 理论系列跨度大，待抽查具体奠基篇目 |
| R14 | Tolman, E. C. (1948). Cognitive maps in rats and men. *Psychological Review*, 55(4), 189–208. + O'Keefe, J., & Nadel, L. (1978). *The Hippocampus as a Cognitive Map*. | 空间隐喻的拒绝依据（design/05） | ✅(Tolman) / 📕(O'Keefe & Nadel) |
| R15 | Miller, G. A. (1956). The magical number seven, plus or minus two. *Psychological Review*, 63(2), 81–97. + Cowan, N. (2001). The magical number 4 in short-term memory. *Behavioral and Brain Sciences*, 24(1), 87–114. | 抗稀释 top-k 上限 | ✅(Cowan) / ⚠️(Miller 经典但本次未查) |

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
| R23 | Russell, J. A. (1980). A circumplex model of affect. *Journal of Personality and Social Psychology*, 39(6), 1161–1178. | V/A 二维情绪模型 | ⚠️ Crossref 未直接命中，待抽查 |
| R24 | Bradley, M. M., & Lang, P. J. (1994). Measuring emotion: The self-assessment manikin and the semantic differential. *Journal of Behavior Therapy and Experimental Psychiatry*, 25(1), 49–59. | SAM 九点量表（标注基准） | ✅ |
| R25 | Watson, D., Clark, L. A., & Tellegen, A. (1988). Development and validation of brief measures of positive and negative affect: The PANAS scales. *Journal of Personality and Social Psychology*, 54(6), 1063–1070. | 效价测量工具 | ✅ |
| R26 | Bower, G. H. (1981). Mood and memory. *American Psychologist*, 36(2), 129–148. | 心境一致性检索加权 | ⚠️ Crossref 仅命中其 1986 综述，1981 原版待抽查 |
| R27 | Mohammad, S. M. (2018). Obtaining reliable human ratings of valence, arousal, and dominance for 20,000 English words. *Proceedings of ACL 2018*. | 文本情绪自动量化词表 | ✅ |

## 业界文献（非学术，一手来源已读全文核实）

| # | 来源 | 获取方式 |
|---|---|---|
| I1 | wast3《Memory Engineering: The Discipline That Decides Whether Your AI Agent Has a Past》X, 2026-08-04 | ✅ 2026-08-08 浏览器直接读全文 |
| I2 | N01ennn《How to be a Memory Engineer, from the perspective of Stanford, Microsoft, Anthropic and Nvidia》X, 2026-08-03 | ✅ 2026-08-08 浏览器直接读全文。⚠️ 文中转述的 Stanford/PlugMem/Memento/Anthropic 数据（47x 能耗差、97% 错误率降幅等）为二手转述，原始论文未逐一核实 |
| I3 | Claude-Mem (github.com/thedotmack/claude-mem) README | ✅ 2026-08-08 直接读取 |
| I4 | MemPalace（本地部署的记忆系统） | ✅ 第一手日常使用观察 |

---

*维护规则：任何文档新增理论引用时必须同步登记本表并标注核实状态；标注 ⚠️ 的条目在后续迭代中逐条抽查转正或替换。*
