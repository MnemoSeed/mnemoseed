# 模型路由配置 UX（Dream LLM configuration · console ⑧ / 首次运行向导 / CLI onboard）

## 1. 问题陈述

没有基础设施背景的用户，在面对梦境 LLM 服务商配置界面时无法一次正确地完成设置。典型卡点：

- 不知道各字段对应什么、该填什么：endpoint 放哪里、API key 放哪里、model 从哪里选（以 Fireworks.ai 为例；换 OpenRouter、Anthropic 时同样无从下手）；
- 选择了 `openai_compatible` 作为连接方式，界面上却依然出现一个 `oauth provider` 输入框——与当前选择无关的死输入，造成明显困惑。

根因：界面说的是代码的术语体系（driver 名、"env var"、"oauth provider"），而不是用户的心智模型（"我有一个 Fireworks 账号——怎么用它？"）；生效默认值不可见；对当前选择用不到的输入框原样显示。

本规格重新设计三个共享同一心智任务的表面——选择服务商、让 MnemoSeed 连上它、验证可用、保存——使首次用户无需任何基础设施背景也能一次走通。

---

## 2. 落地依据（代码现状）

### 2.1 实际存在的驱动（已在 `src/mnemoseed/llm/drivers/` 中核实）

| 驱动 | 是什么 | 它服务的供应商事实 |
|---|---|---|
| `openai_compatible` | `POST /chat/completions`，Bearer key；探测 = `GET /models` | **Fireworks** 与 **OpenRouter**（均为 OpenAI 兼容）以及任何其他兼容端点 |
| `anthropic` | Messages API `POST /v1/messages`，`x-api-key` + `anthropic-version`；探测 = `GET /v1/models` | **Anthropic**（原生，已存在——无需新建） |
| `ollama` | 原生 `POST /api/chat`，无 key；探测 = `GET /api/tags` | 本机 **Ollama**（本地运行、无需账号；"全离线"是角色构成的派生真相，见下注） |
| `oauth` | 复用宿主 `~/.codex/auth.json` / `~/.grok/auth.json`，OIDC 刷新 | 仅限 Codex / Grok 宿主登录（`SUPPORTED_PROVIDERS = ("codex", "grok")`） |
| `stub` | 确定性离线桩（仅测试 / 人工评审阶段使用） | 永不该是用户可见的服务商 |

**不存在** Fireworks 或 OpenRouter 驱动，也**无需**新建——两者都由 `openai_compatible` 承担。原生 `anthropic` 驱动存在。**没有**目录接口；目录搭在探测结果上，成功时返回 `detail["models"]`（`openai_compatible.py:88`、`anthropic.py:95`、`ollama.py:78`）。

**角色模型（最终定案）**：梦境引擎只有两个角色——`deep_reflection`（长背景深睡反思）与 `short_increment`（短增量合并）。两个角色各自**可独立指向任意服务商**（Fireworks / OpenRouter / Anthropic / Ollama / 其他 OpenAI-compatible），云 + 本地混搭是完全合法的配置，永不阻断或羞辱。`local_track` **不再是角色**——仅作为**已弃用配置键**保留：接受但带警告、引擎内无消费者。**离线是派生真相**：当所有已配置角色都解析为本地 `ollama` 驱动时，页面显示 "fully offline" 徽章；任一云角色存在即不显示（不给虚假隐私感）；没有离线开关（§8 D10、§8.1）。

### 2.2 路由 payload 语义（`admin.py:104-130`）

`GET /api/v1/llm/routes` 每角色返回：`driver`、`model`、`base_url`、`api_key_env`、`provider`——但 `base_url`/`api_key_env`/`provider` **仅在显式设置时**返回（`table.get(...)`），因此生效默认值（`https://api.fireworks.ai/inference/v1`、`MNEMOSEED_DEEP_REFLECTION_API_KEY,FIREWORKS_API_KEY` 回退链）对 console 编辑表单**不可见**。用户打开"编辑路由"，看到空白的 base URL 和空白 key 字段，对正在生效的默认值一无所知。启用 SecretStore 路径后（§5、§8 D1），`api_key_env` 也可能返回一条引用（`secrets:mnemoseed/dream/<role>`）——UI 必须把它渲染成"key 已保存"状态（掩码尾缀），而不是空白字段。

### 2.3 死输入的具体位置（exact dead-input）

> 现状代码（2026-08-15）已修复自由文本 `oauth provider` 字段（`llmEditFormHtml` 改为 provider 卡单选，`llmEditorOAuthCards` 三态卡）。**仍存在的向导级缺陷**：`showDreamSetup()`（`app.js:695`）一进向导就并发预拉 `oauth-availability`（行 706），`wizardStep1Html`（行 793）把 `wizardOAuthRows`（行 798）**先于任何选择**渲染——即"codex/grok 未登录状态在用户做任何选择之前就摆出来"。修复见 §10.4。

- **向导（历史缺陷，已随重写消失）**（旧 `dreamSetupHtml`，约 474-520 行）：BYOK 表单无论选中哪个驱动，都固定渲染五个字段，包括 `oauth provider`（占位符 `codex | grok`）。选 `openai_compatible` 它照样显示。
- **console ⑧ 编辑表单（历史缺陷，已修复）**（旧 `llmEditFormHtml`，约 2387-2407 行）：`oauth provider` 文本输入框对每个驱动都渲染，包括 openai_compatible / anthropic / ollama。

存在原因（历史）：`provider` 是真实的路由参数，OAuth 路径需要它。但 UI 把它当作文本字段暴露给所有流程。正确做法：OAuth 路径是**一条独立的路由选择**，不是通用表单上的一个字段（见 §6 / §10.4）。

### 2.4 术语与心智模型错位

- 向导的驱动下拉列出原始名：`anthropic / oauth / ollama / openai_compatible / stub`。用户按品牌思考，不是按驱动。
- 占位符对每个驱动都是 Anthropic 中心的：model `e.g. claude-opus-5`、key `e.g. ANTHROPIC_API_KEY`。`claude-opus-5` 与配置样例里的 `claude-sonnet-5`（`config.py:376`）都是**未经核实的 model id**——默认值里不要放未核实 id。
- 向导**只**配置 `deep_reflection`（`wizardSave` POST 到 `/api/v1/llm/routes/deep_reflection`，app.js:975，可选共享 `short_increment`）；没有任何角色说明，没有选择。
- 连通性失败暴露原始内部信息：`unreachable — {"error":"GET /models returned HTTP 401"}`（console）、`connectivity test failed: GET /models returned HTTP 401`（CLI `llm set`、`onboard`）。没有任何修复指引。
- `stub` 是向导/console 下拉中的合法选项——把测试驱动摆给了用户。

### 2.5 CLI `onboard` LLM 步骤（`onboard/service.py:202-227`）

提示 `llm driver (e.g. ollama, anthropic, stub)` 与 `llm model`——没有 `base_url`，没有 `api_key_env`。因此 Fireworks/OpenRouter/Anthropic 用户**根本无法**从 CLI 配置云服务商（没有 key 环境变量被采集 → 探测 401 → 步骤静默跳过，显示"connectivity test failed"）。提示的示例驱动全是术语，且漏掉了实际默认驱动。

### 2.6 key 存放的两种路径（必须被教会）

**主路径——SecretStore 文件后端，免重启。** API key 可在 console ⑧ / 向导 / CLI 里**粘贴一次**；守护进程把它写入 `~/.mnemoseed/secrets/<role>.key`（POSIX：文件 0600、目录 0700；Windows：用户配置文件 ACL）。配置里只存一条引用 `secrets:mnemoseed/dream/<role>`——key 永不进入 settings DB、永不回显到 UI。改动**无需重启守护进程**即生效：路由按 generation 重新解析，key 变更被立即拾取（generation-bump re-resolve）。key 只在粘贴的那一次可见；此后仅显示掩码尾缀 `****1234`，可一键删除。

**次路径——环境变量（headless/CI）。** 环境变量名仍然受支持，供无头部署与 CI 使用（12-factor 惯例）。诚实的代价：`RoleRouter.resolve()` 在**首次物化**时从**守护进程进程环境**读取 key 并缓存实例（`routing.py:56-88`），因此在 *新终端* 里设置的**新环境变量值**对 *已在运行的* 守护进程不可见（Windows 的 `setx` 同理只影响后续新进程）；修复方式 = "设置变量，然后重启守护进程"。此代价现在只是**可选的**——有交互界面的用户走主路径即可免重启；无头环境默认承担它。

UI 必须同时教会这两条路径：交互界面优先引导主路径（粘贴即生效）；环境变量路径只在无头/脚本场景出现；教学块绝不再对 key 变更说"必须重启"（只对环境变量路径如实说明）。行业依据（已核实）：Codex CLI `~/.codex/auth.json`、gh keychain 回退文件 + `GH_TOKEN`、Docker `config.json` base64、12-factor 环境变量。

### 2.7 文档 vs 代码漂移（必须解决，而非绕过设计）

| 承诺 | 代码现实 |
|---|---|
| FR-6.9：向导顺序 ① OAuth ② BYOK ③ 离线轨（*定案后改写*：③ 离线轨并入服务商卡片——Ollama 卡 + 质量提示，无独立离线序列，见 §8.1） | 向导把 OAuth 与 BYOK 并排展示；"use X OAuth" 按钮只是预填同一个表单。没有序列化引导。 |
| FR-6.9："中国用户可选 MiniMax/Kimi 等 CLI 服务商，选择时明示数据出境提示" | **未实现。** 没有任何 MiniMax/Kimi 服务商，任何地方都没有出境提示。 |
| FR-6.9："Anthropic 订阅明确不做" | 代码正确——`oauth` 只支持 codex/grok；`anthropic` 仅 key。一致。 |
| design/02 §6：默认 deep_reflection → Kimi K3（Fireworks），short_increment → DeepSeek V4 Flash（Fireworks）；**local_track 默认路由删除**（角色模型定案，§8 D10） | 与 `DEFAULT_LLM_ROUTES`（`config.py:138-164`）一致；当前仍含 local_track 条目，工程批次一并移除（§8.1）。Fireworks model id 已在配置注释中核实；视为可信默认。 |
| PRD-07 G-AC2：⑧ 配置全部两个角色（deep_reflection / short_increment） | 是；**向导**只配置 deep_reflection（+ D4 共享复选框）。有意的但未成文——见 §8 决策 D4。 |

---

## 3. 设计：一个"provider-first 路由配置器"组件

一个组件统领全部三个表面（§10）。它的职责只有一件事：**"我有一个服务商账号——把它配好，让我的梦境能跑起来。"** 它从不同用户索要 driver 名，对当前选择用不到的字段绝不显示，也绝不让任何默认值隐藏。

### 3.1 第一步——服务商选择器（品牌优先，驱动无关）

**先有角色，再有服务商。** 服务商卡片只回答"用哪个服务商"，不回答"干什么"——它们对两个梦境角色（`deep_reflection` / `short_increment`）同样有效，被编辑的是**正在编辑的那个角色**。⑧ 编辑器入口永远是角色卡片（§4 的一句话说明）：点 "Edit route" 后，该角色的路由编辑器才展开服务商选择器；向导则默认编辑 `deep_reflection`，配 D4 定案的"also apply to short_increment"共享复选框。本设计**没有第三个角色、也没有"第三张卡"**——不存在 `local_track` 角色卡（§2.1 角色模型）。

一组单选卡片，每张卡对应一条可用路径。每张卡用一句话说明"你需要什么"：

```
◎ Fireworks                     OpenAI-compatible · 按量付费 · 约 1,000 个模型
   "Best starting point — MnemoSeed's recommended models run here."
○ OpenRouter                    OpenAI-compatible · 一把 key，很多模型
   "One API key for hundreds of models from many labs."
○ Anthropic / Claude            原生 API · 需要一个 Anthropic API key
   "For Claude models (claude-opus / claude-sonnet class)."
○ Ollama on this computer       本机 · 免费 · 无需账号
   "Runs fully offline on this machine. Lower synthesis quality."
○ Another OpenAI-compatible API 你选的端点
   "Point at any other /chat/completions endpoint (e.g. a company gateway)."
```

内部映射（对用户绝不原样展示，但复用于文案）：

| 卡片 | driver | base_url（预填、可编辑） | key 来源（预填、可编辑） |
|---|---|---|---|
| Fireworks | openai_compatible | `https://api.fireworks.ai/inference/v1` | `FIREWORKS_API_KEY` |
| OpenRouter | openai_compatible | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` |
| Anthropic | anthropic | `https://api.anthropic.com` | `ANTHROPIC_API_KEY` |
| Ollama | ollama | `http://localhost:11434` | （无——不需要 key） |
| 其他 | openai_compatible | 空 → 必填 | 默认 `MNEMOSEED_DEEP_REFLECTION_API_KEY` |

key 来源列是环境变量名（headless/CI 路径）；用户也可选择**粘贴 key**，改由 SecretStore 本地落盘（§2.6、§5）——两路都有效。

**OAuth 路径是服务商选择之外的双路径入口**（向导与 ⑧ 编辑器一致，见 §6）：宿主登录卡片（Codex / Grok，按 `oauth-availability` 三态渲染）+ "改为粘贴 token"路径——永远不是自由文本字段。

### 3.2 第二步——变形表单

表单体随选择变化（渐进披露）：

- **key 字段只为需要 key 的服务商渲染。** Ollama 时 key 块消失。
- **API key 字段支持两种输入**：(a) **粘贴 key**（主路径——"永不索要 key 值"的旧红线由 SecretStore 取代：值只经本地通道交给守护进程一次，写入 `~/.mnemoseed/secrets/<role>.key`，此后永不再显示，仅掩码尾缀 `****1234`，可删除）；(b) **环境变量名**（headless/CI 路径，§2.6）。字段下方是一个可折叠的、按操作系统拆分的"如何设置"教学块（§5）。
- **base_url** 预填且可编辑，带一键"重置为 <服务商> 默认"。在引导表面收进"Advanced: endpoint"；在 ⑧ 编辑器完全展开。
- **model** 是**服务商作用域的模型选择器**（§3.4）：live catalog（来自探测结果 `detail.models`）+ 探测前的服务商精选建议 + **Load model list** 按钮（主动拉取该端点目录，无需先跑探测）+ 自由输入始终允许。Ollama 的目录来自 `GET /api/tags`，模型缺失时给出"先 pull"提示（`ollama pull llama3.1:8b`）。

### 3.3 第三步——角色分配 + 测试 + 保存

向导用一句话说明它要配置的角色（§4），⑧ 编辑器在每个角色卡片上重复这句话。然后：**Test connection** → 通过则**启用 Save**，探测失败则保留表单并显示修复指引（§7）。

### 3.4 服务商作用域的模型选择器（业界模式参考）

模型选择做成"服务商作用域"——只列该服务商在该端点上的模型——是当前 IDE / 聚合器的事实标准。两个参考实现（已核实，URL 记录在 §12）：

- **OpenRouter** 模型目录页（https://openrouter.ai/models ）：按服务商聚合的 live catalog + 即时搜索/过滤 + 每行可复制 model id；目录按服务商命名空间（`provider/model`），与 `GET /api/v1/models` 的返回一致。
- **Cursor** 模型页（https://cursor.com/docs/models ）：展示"一键选择的模型集合 + 定价表 + 每个模型的可读说明与切换入口"，用户从集合里选，而不是手敲 model id。

据此定下的三条行为（已在 §3.2 / §7 落地）：(1) 目录按服务商 + 端点作用域，绝不混入别的服务商的模型；(2) live catalog 通常上千条，组合框提供本地搜索/过滤；(3) 自由输入始终可用——目录只是加速器，不是约束。

---

## 4. 各角色指引（每个角色一句话，常显）

用于 ⑧ 页（角色卡片副标题）与向导（一行说明）。

梦境引擎只有两个角色（§8 D10）。每个角色一句大白话说明，⑧ 页作为角色卡片副标题、向导作为一行说明。两个角色可各自独立选择服务商，互不绑定——云 + 本地混搭是正常用法。

| 角色 | 一句话说明（UI 字符串） | 推荐搭配 |
|---|---|---|
| deep_reflection | "The careful model. Reads your recent sessions and writes the distilled facts into long-term memory. Use the strongest model you can afford here." | Fireworks kimi-k3（默认）· Anthropic claude-opus 档 · 预算内任何强云模型（可选 Ollama） |
| short_increment | "The quick model. Handles the frequent small consolidation passes. Use a fast, low-cost model." | Fireworks deepseek-v4-flash-0731（默认）· 快速/低成本云模型（可选 Ollama） |

**质量提示规则**：任一角色**选择 Ollama** 时，紧贴卡片/表单显示一行质量提示——`Lower synthesis quality than cloud models — you accept this for privacy or cost.` 不阻断、不二次确认，只是告知；向导、⑧ 编辑器、CLI 三处一致（§11）。两个角色都指向 Ollama = 全离线，页头显示派生徽章（§9、§10.1）。

出现以下术语时必须带 tooltip/展开器：**endpoint**（"服务商接收 MnemoSeed 请求的地址"）、**env var**（"存于你电脑环境里的具名值——无头/CI 路径下 MnemoSeed 从它读 key；交互界面首选把 key 直接交给 MnemoSeed 本地保存"）、**context / max tokens**（"模型单次允许产出的文本量"）、**OpenAI compatible**（"Fireworks 与 OpenRouter 说的同一种 API 方言——一条代码路径即可通吃"）。

---

## 5. API key 教学块（"key 到底放哪"的答案）

渲染在 key 字段下方，所有需要 key 的服务商都显示。**主路径是粘贴一次、免重启**（SecretStore 文件后端，§2.6、§8 D1）；环境变量是 headless/CI 回退。布局（console/向导）：

```
API key
[ FIREWORKS_API_KEY        ]  ← env-var name (headless/CI)  ·  or paste a key once
[ •••••••••••••••••1234    ]  ← paste here once — never shown again
                               (key saved — ****1234)  [delete]

Paste your key once. MnemoSeed stores it locally under ~/.mnemoseed/secrets and
never displays it again — only this masked tail (****1234) is shown. It is never
written into settings, never uploaded to any MnemoSeed server, and you can
delete it any time. Changes apply immediately — no daemon restart.

1. Create a key:  https://app.fireworks.ai/settings/users/api-keys   [open]
2. Paste it here — or, for headless/CI, set it as an env var instead:
   (env fallback below; a NEW env value still needs a daemon restart)
```

行为要点：

- **粘贴路径（主）**：key 只在粘贴的那一次可见；此后仅显示掩码尾缀 `****1234`，保存 chip 带 `delete`，删除后回到空粘贴态。改动立即生效，**不要求重启守护进程**。
- **环境变量路径（回退，headless/CI）**：环境变量名仍受支持（12-factor 惯例）。诚实代价保留：`setx` / 新终端里的**新**值对运行中的守护进程不可见——只有环境变量路径下变更 key 才需要"设置变量并重启守护进程"（§2.6）。教学块用大白话直说这一点，并由探测（§7）确认可见性（401 ⇒ 重贴 key 或修环境变量）。
- 文案是**按服务商定制**的：key 创建 URL、标准环境变量名、精确命令。macOS 与 Windows 各带自己的命令标签页；Windows GUI 用户永远看不到纯 bash 指令，反之亦然。

服务商快速上手事实（已对照官方文档核实；URL 记录在 §12）：

| 服务商 | key 创建 | 环境变量 | base_url | 目录接口 |
|---|---|---|---|---|
| Fireworks | app.fireworks.ai/settings/users/api-keys | `FIREWORKS_API_KEY` | `https://api.fireworks.ai/inference/v1` | `GET /models` |
| OpenRouter | openrouter.ai（账号 → keys） | `OPENROUTER_API_KEY` | `https://openrouter.ai/api/v1` | `GET /api/v1/models` |
| Anthropic | platform.claude.com/settings/keys | `ANTHROPIC_API_KEY` | `https://api.anthropic.com` | `GET /v1/models` |
| xAI（Grok） | console.x.ai（API Keys 页，需登录） | `XAI_API_KEY` | `https://api.x.ai/v1` | `GET /models`（OpenAI 兼容） |
| Ollama | 无 | 无 | `http://localhost:11434` | `GET /api/tags` |

---

## 6. OAuth 可见性逻辑（消灭死输入）

> **向导侧已被 §10.4 取代**（连接方式先行 → 选①宿主登录后才探测 codex/grok；本章的"面板渲染在服务商卡片上方"仅适用于 ⑧ 编辑器）。⑧ 编辑器侧本节仍然有效。

**规则：OAuth 控件只在 OAuth 路径真正被提供时出现，且 `provider` 值只能由专用控件设置——绝不来自自由文本字段。OAuth 双路径（宿主登录卡片 / 粘贴 token）在向导与 ⑧ 编辑器中一致存在。**

1. **永不**在任何 BYOK/驱动表单里渲染自由文本的 `oauth provider` 字段。从 `dreamSetupHtml` 与 `llmEditFormHtml` 中移除。
2. **宿主登录卡片（向导与 ⑧ 编辑器一致）**——"复用本机登录"面板渲染在服务商卡片上方，只列出 `oauth-availability` 报告的 Codex / Grok 宿主登录，每个服务商一张卡片、三种状态：
   - `present && !expired` → **可选卡片** `Use Codex login`。点击进入 **OAuth mode**：driver=oauth、provider=codex、model 字段保留，base_url 与 key 字段隐藏，banner："MnemoSeed will use the Codex login on this machine — no key needed. It refreshes itself while you're signed in."
   - `present && expired` → **禁用卡片** "log in again first"：文案给出**精确 CLI 命令**（Codex：`codex login`，已对照官方文档核实）并可一键复制；该路由的保存被拦，直到重新登录后返回。
   - `!present` → **禁用卡片** "log in to the <provider> CLI first"（Codex 宿主登录在 `~/.codex/auth.json`，Grok 在 `~/.grok/auth.json`）。
   - OAuth mode 的 model 字段保留精选建议（`gpt-5.6-codex` 等在发布前必须**核实**——不要发布当前这个未核实占位符）。
3. **粘贴 token 路径（第二条路，向导与 ⑧ 编辑器一致）**——"or paste a token instead"：以 key 端点把 token 写入 SecretStore（与 §5 同一机制），附**官方文档链接**：Codex → https://developers.openai.com/codex/auth（API-key 登录小节，已核实）；Grok → https://docs.x.ai/developers/quickstart（API Keys 页在 https://console.x.ai/team/default/api-keys，官方文档指向；console 需登录，标记"入口页"）。宿主未登录时，OAuth 服务商仍可由此配置。
4. **保存门槛（仅该路由）**——OAuth 路由在登录不可用（expired / absent）时**保存被拦**，只针对那条路由，不波及 BYOK 卡片；被拦文案给出修复路径（重新登录或粘贴 token，见 §11）。
5. **CLI**——`--provider` 仍是 `llm set` 的合法 flag（脚本化对齐），但交互式 `onboard` 向导绝不把它当自由文本问；而是把检测到的登录列为编号选项，并提供"粘贴 token"替代项。

每个字段 × 每个服务商选择的决策表（实现者的单一事实来源）：

| 字段 | openai_compatible（Fireworks/OR/其他） | anthropic | ollama | oauth mode |
|---|---|---|---|---|
| API key（粘贴 / 环境变量名） | ✅ 可见，预填 | ✅ 可见，预填 | 隐藏 | 隐藏 |
| base_url | ✅ 可见（高级） | ✅ 可见（高级） | ✅ 可见（高级） | 隐藏 |
| model | ✅ 可见 + 目录 | ✅ 可见 + 目录 | ✅ 可见 + 目录 | ✅ 可见（建议） |
| oauth provider 文本字段 | **绝不** | **绝不** | **绝不** | **绝不**（仅控件设置） |
| OAuth 宿主登录卡片（Codex/Grok，三态） | ✅ 顶部区 | ✅ 顶部区 | ✅ 顶部区 | 已选中 |
| max tokens（仅 ⑧） | ✅ 高级 | ✅ 高级 | 隐藏 | ✅ 高级 |

---

## 7. 探测 / 测试 UX（大白话，先给修复）

### 7.1 状态与文案

| 探测结果 | 呈现（UI 字符串） |
|---|---|
| 进行中 | `Testing connection to Fireworks…`，标准 loading 样式，按钮禁用 |
| 成功 | `Connected to Fireworks — key in FIREWORKS_API_KEY works. Found 1,204 models.`（绿色）。model 下拉从 `detail.models` 填充；**Save route** 武装。 |
| 探测成功但目录为空 | `No models listed — pick a suggestion, type the exact model id, or use Load model list.` |
| 401 / 403 | `Fireworks rejected the key in FIREWORKS_API_KEY. It's missing, wrong, or expired — check it at <provider key URL>, then paste a new key here.`（环境变量路径下补一句"修好后重启守护进程"） |
| 连接被拒 / DNS（Ollama） | `Can't reach Ollama at http://localhost:11434. Is the Ollama app running? Install from ollama.com, then pull a model (ollama pull llama3.1:8b).` |
| 连接被拒 / DNS（云） | `Couldn't reach <provider>. Check your internet connection or firewall, then try again.` |
| 超时 | `Timed out talking to <provider>. The endpoint may be slow or blocked — check <endpoint> and try again.` |
| 未知 driver（UI 下不应出现） | `That connection type isn't built in — go back and pick a provider.` |
| 未通过探测就保存被拦 | `Test the connection first — a route can only be saved after it works.` |

### 7.2 行为

- 探测失败**保留全部字段**；什么都不丢。修复块内联、聚焦，精确指向要改的字段。
- 401 情况复用 §5 的 key 教学块（折叠），直接导向"粘贴新 key"（主路径）或"修环境变量并重启"（headless 路径），用户无需离开表单、无需重启。
- 成功时刷新目录：model 选择器从探测结果的 `models` 列表重新填充（无需后端改动——它已经搭在 `detail["models"]` 上）。目录刷新另有 **Load model list** 按钮：主动拉取该端点模型列表，无需先跑探测；加载中按钮转 spinner 并禁用（§3.2、§3.4）。更干净的长期方案见 D2。
- 旧的原始渲染（`reachable — {"error":...}` JSON、"unreachable" 徽章）处处替换，包括 ⑧ 角色卡片探测徽章 → 改为 `connected` / `needs attention`，卡片上显示同一条大白话消息。

---

## 8. 需要拍板的决策（orchestrator / 产品）

> D1 已定案：**SecretStore 文件后端**——API key 可粘贴一次，写入 `~/.mnemoseed/secrets/<role>.key`（POSIX 文件 0600、目录 0700；Windows 用户配置文件 ACL）；settings DB 仍是设置主存储并预留 scope 列；配置只存引用 `secrets:mnemoseed/dream/<role>`；改动**免重启**生效（generation-bump 重新解析）。环境变量名仍受支持（headless/CI）。SaaS key 托管推迟到 TEE 里程碑。

| # | 问题 | 选项 | 建议 |
|---|---|---|---|
| D1 | key 处理：纯环境变量（现状）vs"粘贴 key 由 MnemoSeed 写入用户环境变量 / 系统凭据库"？ | (a) 纯环境变量 + 教学（现状）；(b) 从 console 写一个 `~/.mnemoseed/.env` 或 OS keychain 条目；(c) 完整 OS 凭据库集成 | **已定案**——SecretStore 文件后端（粘贴一次、本地落盘、免重启）+ 环境变量回退（headless/CI）；见上注。行业先例（已核实）：Codex CLI `~/.codex/auth.json`、gh keychain 回退文件 + `GH_TOKEN`、Docker `config.json` base64、12-factor 环境变量。 |
| D2 | 实时模型目录：复用探测 `detail["models"]`（后端零改动）vs 新建 `GET /api/v1/llm/catalog?driver=&base_url=` 接口？ | (a) 仅探测；(b) 专用目录接口 | **(a) 本轮**——先交付 UX；(b) 作为发布打磨后续（探测是按需的，首次访问在用户测试前看不到目录——happy path 可接受）。 |
| D3 | 原生驱动：无需新建——Fireworks/OpenRouter = openai_compatible，Anthropic 原生，Ollama 原生。确认？ | — | **确认；无需驱动工作。** |
| D4 | 向导角色范围：仅 deep_reflection（现状）vs "同时用于 short_increment" 复选框（写两个角色）vs 让向导配置全部两个？ | (a) 现状；(b) +共享复选框；(c) 完整双角色向导 | **(b)**——一个复选框、一行文案，覆盖常见的"一把 key、一个服务商"用户，又不把 TTFM 拖过 3 分钟；每个角色后续都可在 ⑧ 独立改（含改指向 Ollama，见 D10）。 |
| D5 | 把 `stub` 驱动从向导/console 下拉隐藏（保留在 API 与配置里供测试）？ | (a) 隐藏；(b) 保留 | **(a) 隐藏**——测试接缝不是用户路径。 |
| D6 | MiniMax/Kimi 出境路径（FR-6.9）：实现，还是删掉承诺？ | (a) 在"Other OpenAI-compatible"卡片上加"中国区域"说明，附数据出境提示；(b) 实现前从文档删除 | **(a)**——零代码、一条提示，补回一个已文档化的承诺；提示写明"记忆会出境到服务商服务器"。 |
| D7 | `onboard` CLI LLM 步骤：扩展为采集 base_url + api_key_env + 服务商选择？ | (a) 是，镜像组件；(b) 保持 driver+model | **(a)**——不这样，今天 CLI 根本无法配置云服务商（见 §2.5）。 |
| D8 | 探测错误分类：前端解析字符串（现状）vs 后端新建结构化 `error.kind`？ | (a) 前端解析；(b) 后端 kinds | **(a) 现在，(b) 以后**——§7.1 的三四种错误类稳定，与现有字符串匹配。 |
| D9 | 核实 model id：当前占位符/`default_config_toml` 样例（`claude-opus-5`、`claude-sonnet-5`）未核实。 | (a) 只从目录取，不发布未核实 id；(b) 对照服务商文档核实 | **(a)+(b)**：发布时用目录核实过的 id 替换未核实 id；Fireworks 默认值（配置注释已核实）维持不变。 |
| D10 | 角色模型（**已定案**）：梦境引擎只有两个角色 `deep_reflection` / `short_increment`，各自可**独立指向任意服务商**（Fireworks / OpenRouter / Anthropic / Ollama / 其他 OpenAI-compatible）——云 + 本地混搭是完全合法的配置，永不阻断或羞辱。`local_track` 不再作为角色，仅保留为**已弃用配置键**（接受 + 警告，引擎内无消费者，无角色卡）。离线 = **派生真相**：所有已配置角色都解析为本地 ollama 驱动时显示 "fully offline" 徽章；任一云角色存在即不显示；没有离线开关。 | — | **已定案**——§2.1、§3.1、§4、§9、§10 已按此改稿；文档同步见 §8.1。 |
| D11 | 权限范围（**已定案**）：模型路由（及引擎设置）**系统级**，仅 owner/admin 级可配置——自托管 = owner 账号（开源单用户构建的唯一账号）；商业多用户 license = admin 级、作用于所有用户；SaaS = 云 Admin Plane（系统操作员级）、作用于所有用户。**不是**用户个人设置。 | — | **已定案**——⑧ 页权限模型见 §10.1.1。 |

### 8.1 文档同步清单（engineering batch 一次性落实）

> 角色模型与权限范围定案后，下列文档随工程批次一次性同步（逐条一行，均指向本规格对应章节）：

- **PRD-02 FR-2.7**：离线轨改写为"两个角色都指向 Ollama"——移除"离线轨"作为独立第三选项的表述。
- **PRD-02 FR-2.14**：`LLM_ROLES` = 两个角色（`deep_reflection` / `short_increment`）；`local_track` 降级为**已弃用配置键**（接受 + 警告、引擎无消费者）。
- **design/02 §6 默认值**：删除 `local_track` 默认路由，仅保留两个角色的默认。
- **PRD-06 FR-6.9**：离线选项 ③ 并入服务商卡片（Ollama 卡 + 质量提示），删除"引导式离线轨"序列。
- **PRD-07 G-AC2**："全部三个角色" → "全部两个角色"（`deep_reflection` / `short_increment`）。
- **design/07 §8**：梦境路由表由三行改为两行（`local_track` 行删除）。
- **CLI `llm` help 文本**：角色说明改为两个角色，移除 `local_track` 示例。
- **onboard LLM 步骤文案**：与 §10.3 / §11.3 对齐——服务商选择含 Ollama 质量提示、双角色说明。

---

## 9. 空态 / 错误态 / 加载态 + 无障碍

### 9.1 各表面状态

| 状态 | 行为 |
|---|---|
| 加载（向导/⑧） | 沿用现有 `Loading…` 骨架，带角色/服务商占位；绝不让页面空白。 |
| `oauth-availability` 拉取失败 | **向导**：连接方式卡③（API key / Ollama）不受影响，选①宿主登录时才拉取（§10.4）——拉取失败时该卡下方显示一行错误 + 重试，BYOK 仍可用。**⑧**：OAuth 卡片区隐藏，服务商卡片 + BYOK 仍可用。 |
| 未检测到任何服务商（向导） | **只在选①宿主登录后**出现（§10.4），一行置灰文字："No Codex/Grok login detected on this machine — you can still use an API key below." |
| 探测成功但目录为空 | model 选择器回退到精选建议 + 自由输入；提示 "The catalog returned no models — pick from the suggestions or type the exact model id." |
| 模型列表加载中（Load model list） | 按钮转 spinner 并禁用；成功后组合框填充该端点 live 目录；失败回到精选建议并提示。 |
| 粘贴的 key 已保存 | 显示 chip `key saved — ****1234`（掩码尾缀）+ `[delete]`；删除后回到空粘贴态；改动立即生效，无需重启。 |
| daemon 宕机 / 拉取失败（⑧） | 沿用现有错误面板 + Retry，不变。 |
| 保存 → 409（test-required 竞态） | 映射为大白话 "Test the connection first"，绝不给原始 409 详情。 |
| 路由卡片无显式配置（⑧） | 用 "defaults" 徽章代替空块——生效 base URL / key 链 / model 现在在卡片上可见，而非只有编辑时可见。 |
| 全离线派生徽章（⑧ 页头 / 路由卡片） | 仅当**所有**已配置角色都解析为本地 ollama 驱动时显示 `fully offline — nothing leaves this machine`；任一云角色存在即**不**显示（混搭绝不显示——不给虚假隐私感）。没有离线开关。 |

### 9.2 无障碍

- 服务商卡片是带可见标签的单选输入（不是纯点击 div）；完整键盘支持；`aria-checked` + 焦点环。
- 所有 `output.feedback` 区域保持 `aria-live="polite"`；探测/保存反馈会被朗读。
- 成功/失败绝不止颜色：图标 + 文字 + 消息。
- 所有表单控件都有真实 `<label for>` 配对（`app.js` 已用此模式）；新变形表单保持——隐藏字段在出现时仍是带标签的字段。
- 可折叠教学块用原生 `<details>`/`<summary>`（焦点/键盘免费）。
- 环境变量命令块渲染为 `<pre>` 加复制按钮（`navigator.clipboard`，token 复制已用），`aria-label` 包含变量名。
- 对比度与 reduced-motion 遵循 `styles.css` 约定；无新动画。

---

## 10. 表面映射（一个模式，三种渲染）

### 10.1 console ⑧ Models & Routing（完整编辑器）

```
┌─ models & routing ───────────────────────────────────────────────────┐
│  per-role dream models: what each role does, and which model serves   │
│  it. Key values never appear here — only env-var names or a masked    │
│  key tail (****1234).                                                  │
│                                                                       │
│  fully-offline badge (derived — shown only when BOTH roles resolve    │
│  to local ollama;  ⇢ 本页为云+本地混搭 Fireworks+Ollama，故不显示):   │
│  ◉ fully offline — nothing leaves this machine                        │
│                                                                       │
│  host logins: [codex: logged in] [grok: not detected]                 │
│  OAuth dual path: [Use Codex login] (card) · or [Paste a token        │
│  instead]; expired → "log in again first" + codex login; absent →     │
│  disabled card "log in to the <provider> CLI first"                   │
│                                                                       │
│  ┌─ deep_reflection ── the careful model ──────────────────────────┐  │
│  │  connected · Fireworks · accounts/fireworks/models/kimi-k3      │  │
│  │  key: MNEMOSEED_DEEP_REFLECTION_API_KEY → FIREWORKS_API_KEY     │  │
│  │  base URL: https://api.fireworks.ai/inference/v1  ·  max 2048   │  │
│  │  [test connection] [edit route]                                 │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│  ┌─ short_increment ── the quick model ────────────────────────────┐  │
│  │  connected · Ollama · llama3.1:8b                               │  │
│  │  quality note: lower synthesis quality than cloud models —      │  │
│  │  you accept this for privacy or cost.                           │  │
│  │  [test connection] [edit route]                                 │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  [edit route] expands the provider-first form (§3) inline:           │
│    role card → provider picker (cards apply to whichever role is      │
│    being edited) → morphing form → test → save (armed only after      │
│    a passing probe of the exact values)                               │
│                                                                       │
│  foot: Model routing is system-scoped — set by the owner/admin and    │
│        applies to every user.                                         │
└───────────────────────────────────────────────────────────────────────┘
```

#### 10.1.1 权限模型（系统级，非用户级）

模型路由与引擎设置**系统级**，仅 owner/admin 级可配置（§8 D11）：

- **自托管**（开源单用户构建）：owner 账号是唯一账号，owner 即配置者。
- **商业多用户 license**：admin 级，作用于所有用户——普通用户看不到路由、不可改设置。
- **SaaS**：云 Admin Plane（系统操作员级）配置，作用于所有用户；**不提供**用户级个人路由设置。

⑧ 页页脚常显一行：`Model routing is system-scoped — set by the owner/admin and applies to every user.` 普通用户打开 ⑧ 时整页只读（§9.2 的键盘/焦点纪律同样适用——无编辑入口即是只读信号）。

### 10.2 首次运行向导（owner 创建后）

> **已被 §10.4 取代**（连接方式先行）。本节保留作历史/参考，工程实现以 §10.4 为准。

```
┌─ dream model ───────────────────────────────────────────────────────┐
│  "Pick the model that distills your sessions into long-term memory. │
│   One model gets you started — change any role later in Models."    │
│                                                                      │
│  step 1  provider   step 2  key + model   step 3  test & save       │
│                                                                      │
│  ○ Fireworks (recommended)   ○ OpenRouter   ○ Anthropic   ○ Ollama  │
│  ○ Another OpenAI-compatible endpoint                                │
│                                                                      │
│  ── or reuse a login on this computer ──                             │
│  [Codex: logged in] → [Use Codex login] · or [Paste a token instead] │
│  (expired → "log in again first" + codex login; absent → disabled    │
│  card)                                                               │
│                                                                      │
│  [continue]                                            [skip for now]│
└───────────────────────────────────────────────────────────────────────┘
```
服务商卡片对两个角色同样有效（§3.1）。第三步的共享复选框（D4 定案）：

```
┌─ step 3 ─ test & save ─────────────────────────────────────────────┐
│  provider: Fireworks                    [x] also apply to           │
│  Testing connection to Fireworks…         short_increment           │
│  ✓ Connected — key in FIREWORKS_API_KEY works.            [save]    │
└───────────────────────────────────────────────────────────────────────┘
```

第二步按服务商变形（§5），第三步跑探测（§7）然后保存。向导默认配置 `deep_reflection`；**"also apply to short_increment" 共享复选框**在同一屏——勾选即把同一服务商 + 同一 key 一并写入 `short_increment`。若两个角色都选 Ollama，共享复选框同样适用，保存后"全离线"徽章随之出现（§9）。"skip for now" 保持 capture-only daemon——明说，而非让用户自行发现。

### 10.3 CLI `mnemoseed onboard` LLM 步骤

镜像同样的步骤为编号提示，适配终端（无单选 UI、无可折叠——平铺文本，命令一行行原文打印）：

```
[llm]
  Pick the model that distills your sessions into long-term memory.
  One model gets you started; change any role later with `mnemoseed llm set`.
  1) Fireworks (recommended)   3) Anthropic       5) other OpenAI-compatible
  2) OpenRouter                4) Ollama on this computer
  provider [1]: 1
  Create a key at https://app.fireworks.ai/settings/users/api-keys
  Paste the key once — stored locally under ~/.mnemoseed/secrets, never
  shown again (masked tail ****1234, deletable). Or set an env var for
  headless/CI use (a NEW env value still needs a daemon restart):
    Windows:  setx FIREWORKS_API_KEY "your-key"
    macOS/Linux: export FIREWORKS_API_KEY="your-key"   # add to ~/.zshrc
  api key (paste once) or env var name [FIREWORKS_API_KEY]:
  key saved — ****1234
  model [accounts/fireworks/models/kimi-k3]:            ← verified default
  (若选 4 Ollama，先打一行质量提示，再进入测试：)
  ⚠ Ollama chosen for this role — lower synthesis quality than cloud
    models; you accept this for privacy or cost.
  testing connection to Fireworks…
  connected — key works. saving…
  also apply to short_increment? [y/N]: y        ← D4 共享复选框的 CLI 形态
  ✓ dream model configured (openai_compatible/accounts/fireworks/models/kimi-k3)
  (skip: entering no model → "capture-only daemon (dreaming disabled)"; 
   --skip llm and --llm-driver/--llm-model scripted flags unchanged)
```

CLI 与 console **共享同一套**后端（先 `POST /api/v1/llm/test`，再 `/api/v1/llm/routes/deep_reflection`）、同一张服务商表、同一条大白话探测文案、同一个默认值出处。术语在首次出现时内联定义（"env var = 这台电脑上一个具名值，MnemoSeed 从它读 key"）。

### 10.4 首次运行向导 IA（新版，取代 §10.2 —— 连接方式先行）

> **取代 §10.2 的向导结构。** 用户的两条真实投诉直接驱动这次重排：(1) "codex/grok 未登录状态在**任何选择之前**就摆出来"；(2) "保存静默无操作"——连接方式的错误必须在一步之遥、一眼可见。核心原则：**选择先行，状态后置；连接方式是一等选择，不是面板。**

#### 10.4.1 四步走（Step 0–3）

```
┌─ step 0 ─ connection ────────────────────────────────────────────────┐
│  How should MnemoSeed reach a model?                                  │
│                                                                       │
│  [ ① Use a login on this computer ]    ⚡ 无死输入：card① 之下此刻     │
│      Reuse a Codex or Grok sign-in.     不渲染任何 codex/grok 状态。   │
│      No key to manage.                  ← 修复点：OAuth 状态只在       │
│  [ ② Bring your own API key ]             选①后才探测/显示            │
│      Fireworks · OpenRouter · Anthropic ·                              │
│      or any OpenAI-compatible endpoint.                               │
│  [ ③ Run locally on this computer ]                                    │
│      Ollama — free, offline, lower synthesis quality.                 │
│                                                                       │
│  ───────────────────────────────────────────────────────────────────  │
│  [Skip for now — capture-only ✓ dream off until you configure a model]│
│  (一等可见的 skip，不在角落；点击后展示确认行)                         │
│  [continue]  (primary，仅在选中一张卡后武装)                           │
└───────────────────────────────────────────────────────────────────────┘
```

- **Step 0 只问一件事：连接方式。** 三张卡（① Host login ② API key ③ Ollama local）+ 一个一等可见的 Skip。**此处绝不渲染 codex/grok 状态**——`oauth-availability` 探测延迟到 ① 被选中之后（§10.4.2），"两个 codex/grok 未登录 plaque" 缺陷被显式移除。
- **每步恰有一个 primary CTA**：Step 0 `continue`、Step 1 `continue`、Step 2 `continue`、Step 3 `save`（探测通过后武装）。back 永远是 secondary。
- **Skip 是一等可见选项**：Step 0 底部整行按钮 + 文案 `Skip for now — capture-only ✓ dream off until you configure a model`，点击后确认行（§11 文案）并直达 capture-only daemon。

#### 10.4.2 Step 1 —— 按连接方式变形

| 选中的卡 | Step 1 内容 | OAuth 探测时机 |
|---|---|---|
| ① Host login | 此刻**才** `GET /api/v1/llm/oauth-availability`（loading 态先于状态渲染）；随后每个检测到的服务商一行三态：live → 可选 `Use Codex login`；expired → `log in first` + 精确命令 + 文档链接 + **粘贴 token** 兜底；absent → 同样"先登录 + 粘贴 token"。 | **选①后立即**，绝不在 Step 0 之前 |
| ② API key | 服务商卡（Fireworks / OpenRouter / Anthropic / 其他 OpenAI-compatible），每张卡附 key 教学（§5）。 | 不探测 |
| ③ Ollama | 直接进 Step 2（无 key、无服务商卡）；Step 2 渲染质量提示 + `ollama pull` 指引。 | 不探测 |

**zero dead inputs 清单（实现时逐条断言）**：Step 0 上没有任何 oauth 行；选②③时绝不出现任何 oauth/粘贴 token 控件；选①时若全部 expired/absent → 每行都给出"登录先行或粘贴 token"的**可行动**路径，绝不出现"只能看不能做"的死卡（§11.1 文案）。

#### 10.4.3 Step 2 —— key + model（按连接方式变形）

- **① Host login**：model 字段 + 精选建议；无 key、无 endpoint。
- **② API key**：key 字段（粘贴一次 / env var 名）+ endpoint（高级）+ model 组合框（curated + 目录）。
- **③ Ollama**：model 字段（curated 列表）+ 质量提示 + `pull` 指引；无 key、无 endpoint。

#### 10.4.4 Step 3 —— test & save

- `Test connection` → 通过后 `save` 武装（探测 signature 门，§7）；**失败时错误行内联显示"先去测试"原因（§7.1）**——绝不静默。
- 共享复选框 `also apply to short_increment`（D4）保留在 Step 3。

#### 10.4.5 与现状代码的差异（为什么是"取代"）

现状 `showDreamSetup()` 在 `app.js:695-727` **一进向导就并发拉取 `oauth-availability`**（行 706）并在 `wizardStep1Html`（行 793）**先行渲染 `wizardOAuthRows`**（行 798）——正是"选择之前就摆出未登录状态"的根因。新版把该探测移入 Step 1 的 ① 分支，Step 0 只渲染三张连接卡 + Skip。§10.2 的向导结构作废；⑧ 编辑器 IA（§10.1）不变。

---

## UI 文案（English —— 产品界面用）

以下为产品 UI 字符串资产，保持英文原文，逐字保留。

### 11.1 向导

**Step 0 · connection（§10.4，取代旧 Step 1 结构）**：
- Step 0 title: `How should MnemoSeed reach a model?`
- Card ① label: `Use a login on this computer` — subtitle `Reuse a Codex or Grok sign-in. No key to manage.`
- Card ② label: `Bring your own API key` — subtitle `Fireworks · OpenRouter · Anthropic · or any OpenAI-compatible endpoint.`
- Card ③ label: `Run locally on this computer` — subtitle `Ollama — free, offline, lower synthesis quality.`
- Skip (first-class, Step 0): `Skip for now — capture-only ✓ dream off until you configure a model`
- Skip confirm: `Skipped — capture-only daemon. Dreaming stays off until you configure a model. Set one any time in Models.`

**Step 1 · host login（仅选①后出现；OAuth 探测此刻才触发）**：
- OAuth loading: `Checking for logins on this computer…`
- OAuth live: `Codex login found — sign in is current.` / button `Use Codex login`
- OAuth expired: `Codex login found but expired — log in again first, then return here.` (card
  disabled) / command line `codex login` (copy-to-clipboard)
- OAuth absent: `Log in to the Codex CLI first, then come back.` (card disabled;
  per-provider: `<provider>` = Codex / Grok)
- Login-first fallback (expired/absent): `Log in first, or paste a token instead.` → official
  doc links: `How to create a Codex token` → https://developers.openai.com/codex/auth ·
  `How to create an xAI API key` → https://docs.x.ai/developers/quickstart
- OAuth blocked-save: `This route can't be saved until a login is available — log in to the
  Codex CLI first, or paste a token instead.`
- OAuth banner (after selection): `Using the Codex login on this machine — no key needed.
  It refreshes itself while you're signed in.`

**Step 1 · API key / Step 2 · key + model（选②后出现）**：
- Title: `dream model`
- Intro: `Pick the model that distills your sessions into long-term memory. One model gets
  you started — you can change any role later in Models.`
- Provider group: `Which provider do you use?`
- Fireworks card: label `Fireworks`, blurb `Recommended starting point — MnemoSeed's default
  models run here.`
- OpenRouter card: label `OpenRouter`, blurb `One API key, hundreds of models from many labs.`
- Anthropic card: label `Anthropic (Claude)`, blurb `Requires an Anthropic API key from
  platform.claude.com.`
- opencode Zen card（Item 2 新增，editor + 向导通用）: label `opencode Zen (host's Go
  subscription)`, blurb `Curated models the opencode team tests and vets, billed through your
  opencode Zen / Go subscription.`（OpenAI 兼容 `https://opencode.ai/zen/v1`，Bearer key 来自
  opencode.ai/auth；复用路径见 keyNote：`~/.local/share/opencode/auth.json` 的 `opencode-go`
  条目）
- Ollama card: label `Ollama on this computer`, blurb `Free and offline. Runs entirely on
  this machine; lower synthesis quality.`（Ollama 走连接卡③直达 Step 2）
- Ollama quality hint (shown when the role being configured picks Ollama): `Lower
  synthesis quality than cloud models — you accept this for privacy or cost.`
- Share checkbox (D4): label `also apply to short_increment`, note `Uses the same provider
  and key for the quick consolidation model.`
- Other card: label `Another OpenAI-compatible API`, blurb `Point at any other endpoint
  that speaks the OpenAI chat API.`
- Key label: `api key` — `paste once (stored locally, never shown again)` / `or an env var
  name for headless/CI use`
- Key teaching intro: `Paste your key once — MnemoSeed stores it locally under
  ~/.mnemoseed/secrets and never shows it again (only the masked tail ****1234). For
  headless/CI you can use an env var instead.`
- Key saved chip: `key saved — ****1234` (button `delete`)
- Key saved note: `Stored under ~/.mnemoseed/secrets. Not shown again; only this masked
  tail. Deletable any time.`
- Delete key confirm: `Delete this key? Routes using it fail until a new key is set.`
- Key 401 fix: `Fireworks rejected the key in FIREWORKS_API_KEY — it's missing, wrong, or
  expired. Check it at <provider key URL>, then paste a new key here.` (per-provider
  substitution; env-var path appends `or fix the env var and restart for headless/CI.`)
- Load model list button: `Load model list`
- Endpoint label: `endpoint` / advanced header: `Advanced: endpoint`
- Endpoint reset: `reset to Fireworks default`
- Model label: `model`
- Model placeholder: `type or pick a model`
- Catalog empty: `No models listed — pick a suggestion or type the exact model id.`
- Probe in-flight: `Testing connection to Fireworks…`
- Probe ok: `Connected — key in FIREWORKS_API_KEY works.`
- Probe saved: `dream model configured: deep reflection → <model>` (shared: `deep
  reflection + short increment → <model>`)
- Skip button (legacy, superseded by Step 0 skip): `Skip for now — capture-only (dreaming stays off)`
- Skip confirm (legacy): `Skipped — MnemoSeed keeps capturing sessions, dreaming stays off until a model is configured. You can set one any time in Models.`

### 11.2 console ⑧

- Page title: `models & routing`
- Page note: `What each role does, and which model serves it. Key values never appear here —
  only env-var names or a masked key tail (****1234).`
- Role subtitles (§4) — two roles only: `deep_reflection` / `short_increment`.
- Offline badge (derived): `fully offline — nothing leaves this machine`（含义：仅当所有已配置
  角色都解析为本地 ollama 时显示；任一云角色存在即隐藏——派生真相，无开关）
- Card quality note (any role on Ollama): `lower synthesis quality than cloud models — you
  accept this for privacy or cost.`
- Permission footnote: `Model routing is system-scoped — set by the owner/admin and applies
  to every user.`（§10.1.1 权限模型）
- Card probe: `connected` / `needs attention` (with the plain message from §7, not raw JSON)
- Card key line: `key: MNEMOSEED_DEEP_REFLECTION_API_KEY → FIREWORKS_API_KEY`
- Card base: `base URL: https://api.fireworks.ai/inference/v1`
- Defaults chip (no explicit config): `defaults`
- Buttons: `Test connection` / `Edit route` / `Cancel edit` / `Save route` (disabled until a
  passing probe of the exact values)
- Save gate error: `Test the connection first — a route can only be saved after a passing
  probe of these exact values.`
- 409 mapped: same as save gate error.
- Editor header: `Edit route — <role>` (deep_reflection / short_increment)
- Provider group in editor: `Which provider?` (same cards, minus "recommended")
- max tokens label: `max tokens` (advanced), note: `blank = role default`
- Saved banner: `route deep_reflection saved — config version <v> (audited)`
- Restart note (env-fallback only): `Note for headless/CI: a NEW env-var value is picked up
  only after the daemon restarts. Pasted keys apply immediately.`
- OAuth line: `host logins: codex — logged in · grok — not detected`
- OAuth card available (⑧ editor): `Use Codex login` (selectable card)
- OAuth card expired (⑧ editor): `Codex login expired — log in again first` + command
  `codex login` (card disabled)
- OAuth card absent (⑧ editor): `Log in to the Codex CLI first` (card disabled;
  per-provider `<provider>`)
- Paste affordance (⑧ editor, Item 1/P1 修复): ONE role-named paste module per role editor —
  summary `Paste the API key for the careful model — once, stored locally, never shown again
  (masked tail ****1234 only).`（short_increment → `the quick model`）; input label/placeholder
  同样以角色 plain-name 命名; oauth 的 "paste a token instead" 折叠进同一模块（选宿主登录卡时
  附该 provider 的官方 token 文档链接）; 整个模块仅对 ollama 隐藏。JH 原始投诉「选 Anthropic 却
  显示 Codex API」根因：旧版 oauth-paste 块以宿主 provider 命名且对非 other 卡可见——已由
  role-named 统一模块取代，paste 目标永远由角色 plain-name 指明。
- Key saved chip (⑧ editor): `key stored — ****1234` + `delete stored key`（掩码尾缀仅来自
  key 端点 masked_tail）; 删除后回退 env 链: `key deleted — this route falls back to its
  env-var chain`
- OAuth blocked-save (⑧ editor): `This route can't be saved until a login is available —
  log in to the Codex CLI first, or paste a token instead.`
- Load model list button (⑧ editor): `Load model list`

### 11.3 CLI（onboard LLM 步骤 + llm set）

- Step header: `[llm]`
- Intro: `Pick the model that distills your sessions into long-term memory. One model gets
  you started; change any role later with 'mnemoseed llm set'.`
- Provider prompt: `provider [1]: ` (list printed as §10.3)
- Key URL line: `Create a key at <url>`
- Key teaching (printed once, §10.3 block): paste-once path + env-var fallback
- `api key (paste once, stored locally) or env var name [FIREWORKS_API_KEY]: `
- `key saved — ****1234`
- `model [accounts/fireworks/models/kimi-k3]: `
- `testing connection to Fireworks…`
- `connected — key works. saving…`
- Share prompt (D4, CLI 形态): `also apply to short_increment? [y/N]: `
- Ollama quality line (provider = Ollama 时先打): `Ollama chosen for this role — lower
  synthesis quality than cloud models; you accept this for privacy or cost.`
- Success: `✓ dream model configured (<driver>/<model>)`
- Fail 401: `error: Fireworks rejected the key — paste a new one, or fix the env var (then
  restart for headless/CI), and re-run onboard (it resumes here).`
- OAuth expired (CLI): `Codex login expired — run 'codex login' first, then re-run onboard
  (it resumes here).`
- OAuth absent (CLI): `No Codex login detected — log in to the Codex CLI first, or paste a
  token instead.`
- Ollama fail: `error: can't reach Ollama at http://localhost:11434 — is it running?
  Install from ollama.com and pull a model (ollama pull llama3.1:8b).`
- Skip: `skipping the LLM wizard: the daemon stays capture-only (dreaming disabled until a
  model is configured)`
- `mnemoseed llm set --help`: driver help updated to `provider (or --provider codex|grok
  for a host login)`; add `--provider-card`? No — keep parity, add examples in help text.
  Help text names the two roles (`deep_reflection` / `short_increment`) only — no `local_track`
  example or role.

---

## 12. 服务商事实核实（存档）

- **Fireworks**：quickstart（key 创建 URL `app.fireworks.ai/settings/users/api-keys`、`setx`/`export FIREWORKS_API_KEY`、OpenAI 兼容 base `https://api.fireworks.ai/inference/v1`、OpenAI SDK 路径隐含 `GET /models`）—— https://docs.fireworks.ai/getting-started/quickstart
- **OpenRouter**：quickstart（base `https://openrouter.ai/api/v1`、`OPENROUTER_API_KEY`、目录 `GET /api/v1/models`、OpenAI 兼容）—— https://openrouter.ai/docs/quickstart
- **Anthropic**：API overview（base `https://api.anthropic.com`、Messages `POST /v1/messages`、`x-api-key` + `anthropic-version`、key 来自 `platform.claude.com/settings/keys`、models `GET /v1/models`）—— https://platform.claude.com/docs/en/api/overview
- **Ollama**：API reference（`POST /api/chat` stream=false、`GET /api/tags`、无鉴权、`model:tag` 命名）—— https://github.com/ollama/ollama/blob/main/docs/api.md
- **Codex / OpenAI 认证**（粘贴 token 的官方文档链接）：`~/.codex/auth.json` 明文凭据缓存、`codex login` 与 `codex login --with-api-key`、API key 创建于 platform.openai.com/api-keys—— https://developers.openai.com/codex/auth
- **xAI / Grok**（粘贴 token 的官方文档链接）：quickstart（`XAI_API_KEY`、base `https://api.x.ai/v1`、API Keys 页 https://console.x.ai/team/default/api-keys——console 需登录，匿名抓取 403，标记"入口页"）—— https://docs.x.ai/developers/quickstart
- **opencode Zen**（Item 2 新增，2026-08-15 核实）：Zen docs（base `https://opencode.ai/zen/v1`、chat completions 端点 `/zen/v1/chat/completions`（OpenAI 兼容）、模型目录 `GET /zen/v1/models`、key 来自 opencode.ai/auth、config 内 model id 写作 `opencode/<id>` 是 opencode 自己的惯例、直接 API 用裸 id）—— https://opencode.ai/docs/zen/ ；模型目录直取 https://opencode.ai/zen/v1/models （公开抓取成功）；models.dev `OpenCode Zen` 条目（api `https://opencode.ai/zen/v1`、npm `@ai-sdk/openai-compatible`、模型 `api.id` 均为裸 id，无 `opencode/` 前缀）与 live 目录一致——卡片用的裸 id 由此双重核实。**本机验证**：`~/.local/share/opencode/auth.json` 存在且含 `opencode-go: {type:"api", key}`（用户 opencode Go 订阅 key，值全程不透出）——复用路径 (b) 成立。
- **OpenRouter 模型目录**（§3.4 模式参考）—— https://openrouter.ai/models
- **Cursor 模型页**（§3.4 模式参考）—— https://cursor.com/docs/models

### 12.1 核实说明（本规格的落地依据）

- 代码阅读：`console/static/app.js`（4648 行，2026-08-15 状态——向导已重构为 `wizardStep1/2/3Html` + `showDreamSetup` 于行 695，⑧ 编辑器 `llmEditFormHtml` 于行 3114；§2.3/§14 的行号以本次阅读为准）、`config.py` `DEFAULT_LLM_ROUTES`、`llm/admin.py` + `admin_routes.py`（仅显式 payload、探测签名、409 门槛化保存）、`llm/routing.py`（env 解析 + 实例缓存）、`llm/drivers/*`（五个驱动，目录在探测 detail 中）、`configwrite/service.py`（env 名校验）、`onboard/service.py`（LLM 步骤）、`cli.py`（`llm status/set`、`onboard`）。
- 本次新增阅读（§10.4/§13/§14 依据）：`app.js` 向导与编辑器全部相关函数（`showDreamSetup` 695-727、`wizardOAuthRows` 749-773、`wizardStep1Html` 793-808、`wizardStep2Html` 854-881、`wizardStep3Html` 883-910、`wizardPayload/Test/Save` 912-1011、`llmEditorProviderCard` 2830、`llmEditorOAuthCards` 2844、`llmRolePasteHtml/llmRolePasteDocs/llmBindRolePaste`（Item 1 取代旧 `llmOauthPasteHtml`/`llmCustomPasteHtml`/`llmBindOauthPaste`）、`llmSyncEditorGate` 2951、`llmEditFormHtml` 3114-3159、`testRoute` 3201、`saveRoute` 3283-3359、`wz-*` 事件 3497-3545）、`styles.css` `:root`（4-25）与向导卡样式（441-458）。`oauth-availability` 在 `showDreamSetup` 行 706 被并发预拉——§10.4 的修复点，源码确认。
- 本机实测：用临时 `MNEMOSEED_HOME` 在备用端口（嵌入式预设，`127.0.0.1:18764`）启动 daemon，走通：owner 设置 → 登录 → `/api/v1/llm/routes`（确认仅显式 payload）→ `/api/v1/llm/oauth-availability`（本机两个登录都已检测到但过期）→ `/api/v1/llm/test` 探测形态（无 key Fireworks 401；离线 Ollama 连接被拒；未知 driver；未探测直接保存 → 409）。在驱动目录里观察到 `stub`，并在线上 payload 中确认默认值不可见。
- 未核实：当前 model 占位符 `claude-opus-5` / 配置样例 `claude-sonnet-5`（D9），以及 OAuth mode 的 `gpt-5.6-codex` 占位符。Grok 宿主登录的重新登录命令（随已装 CLI 而异）未在官方文档核实——UI 以 "log in to the <provider> CLI first" + 粘贴 token 兜底。console.x.ai 需登录（匿名 403），其 API Keys 页 URL 以官方文档指向为准。
- Item 2 备注：opencode Zen 的零复制复用（daemon 直接读 `~/.local/share/opencode/auth.json`）需要后端宿主登录提供者（类比 codex/grok 的 OAuthLLM），本轮前端只做「文档化 + 粘贴一次」——该后端接入留给工程师（见 orchestrator leftovers）。

---

## 13. Design tokens（暗色现代主题，Linear/Vercel 档）

> 响应"整个界面毫无美感"——替换现状 console 的深灰工作台风格为**安静、克制的暗色现代主题**。token 直接替换 `styles.css` 的 `:root`（现状行 4-25），渐进式采纳：新向导/⑧ 编辑器先吃新 token，其余页面随后迁移（§14）。**仅 token 值变化，不改类名契约**——现有 `--bg/--accent/--err/--ok/--warn/--violet` 类名全部保留为别名，兼容不动。

```css
:root {
  color-scheme: dark;
  /* --- 色板（暗色、低饱和、蓝紫 accent）--- */
  --ms-bg: #0b0e14;          /* 页面底 */
  --ms-surface: #10141d;     /* header / 侧栏 */
  --ms-card: #141926;        /* 卡片面 */
  --ms-border: #232b3d;      /* 常规描边 */
  --ms-border-soft: #1b2230; /* 弱描边 / 分隔线 */
  --ms-fg: #e7ecf4;          /* 主文本 */
  --ms-muted: #8a94a8;       /* 次文本 / hint */
  --ms-accent: #6ea8fe;      /* 主 accent（action / 焦点） */
  --ms-accent-soft: rgba(110, 168, 254, 0.13);
  --ms-error: #ff6b6b;       /* 错误 */
  --ms-success: #4ade80;     /* 成功 */
  --ms-warn: #fbbf24;        /* 警示 */
  --ms-ink-on-accent: #0b0e14; /* accent 上的反色（按钮文字） */

  /* --- 类型阶：8 / 14 / 16 / 20 / 28（px）--- */
  --ms-t-xs: 0.5rem;   /* 8  — micro / badge / tile-label */
  --ms-t-sm: 0.875rem; /* 14 — body / field 正文 */
  --ms-t-md: 1rem;     /* 16 — card title / role h2 */
  --ms-t-lg: 1.25rem;  /* 20 — section h2 / 向导标题 */
  --ms-t-xl: 1.75rem;  /* 28 — page h1 */

  /* --- 间距：4-base --- */
  --ms-s-1: 4px;
  --ms-s-2: 8px;
  --ms-s-3: 12px;
  --ms-s-4: 16px;
  --ms-s-6: 24px;
  --ms-s-8: 32px;

  /* --- 圆角 / 焦点环 --- */
  --ms-radius: 8px;
  --ms-radius-sm: 6px;
  --ms-focus-ring: 0 0 0 2px var(--ms-bg), 0 0 0 4px var(--ms-accent);

  /* --- 组件 token --- */
  --ms-card-bg: var(--ms-card);
  --ms-card-border: var(--ms-border);
  --ms-chip-bg: var(--ms-card);
  --ms-chip-border: var(--ms-border);
  --ms-input-bg: #0d1119;
  --ms-input-border: var(--ms-border);
  --ms-btn-bg: var(--ms-card);
  --ms-btn-border: var(--ms-border);
  --ms-btn-fg: var(--ms-fg);
  --ms-btn-primary-bg: var(--ms-accent);
  --ms-btn-primary-fg: var(--ms-ink-on-accent);
  --ms-btn-primary-hover: #84b6ff;
  --ms-btn-primary-disabled: rgba(110, 168, 254, 0.35);

  /* --- 现状类名 → 新 token 的别名（保持既有组件不动）--- */
  --bg: var(--ms-bg);
  --bg-raised: var(--ms-surface);
  --bg-inset: #0b0e14;
  --border: var(--ms-border);
  --border-soft: var(--ms-border-soft);
  --text: var(--ms-fg);
  --text-dim: var(--ms-muted);
  --text-faint: #59647a;
  --accent: var(--ms-accent);
  --accent-soft: var(--ms-accent-soft);
  --ok: var(--ms-success);
  --warn: var(--ms-warn);
  --err: var(--ms-error);
  --violet: #a78bfa;
}
```

### 13.1 状态规则（每个 token 组件的 hover/selected/disabled/error/loading）

| 组件 | normal | hover | selected / armed | disabled | error | loading |
|---|---|---|---|---|---|---|
| 连接卡 / 服务商卡 `.wizard-provider-card` | `--ms-card-bg` + `--ms-card-border` | `border-color: var(--ms-accent)`；`--ms-accent-soft` 底 | `border: 2px var(--ms-accent)` + `--ms-accent-soft` 底 | `.muted`：opacity .6、cursor default、hover 不变 | 卡内容含 `--ms-error` 文本行 | 骨架占位（§9） |
| 主按钮 `.btn-primary` | `--ms-btn-primary-bg` | `--ms-btn-primary-hover` | — | `--ms-btn-primary-disabled`，cursor not-allowed | — | 文字侧加 spinner / `Testing…` |
| 次按钮 `.btn` | `--ms-btn-bg` + border | `--ms-accent-soft` 底 | — | opacity .5 | — | 同主按钮 |
| 输入 `.field input` | `--ms-input-bg` + `--ms-input-border` | `border-color: var(--ms-accent)` | focus：`--ms-focus-ring` | 禁用输入 opacity .5 | `border-color: var(--ms-error)` + 错误文本 | — |
| chip / badge | `--ms-chip-bg` + border | 不变 | `.badge-ok`/`.badge-err` 换色 | 灰 | 红 | — |
| 反馈行 `.feedback` | — | — | — | `--ms-error` 底 + 文本 | — | spinner |

- **focus 纪律**：所有可聚焦控件 focus 时应用 `--ms-focus-ring`（键盘可达，`aria-checked` 卡片同规则）。
- **语义色绝不停留在颜色上**：success/error/warn 必有图标或文字前缀（§9.2 已有约定，token 化后保持）。
- **accent 对比**：`--ms-accent #6ea8fe` 在 `--ms-bg #0b0e14` 上对比度 > 7:1（WCAG AA 达标）；`--ms-btn-primary-fg` 用深色反白保证按钮内对比。
- **加载态骨架**：向导 Step 1 的 OAuth loading 用两行 `.wizard-provider-card` 占位（`--ms-card` + 60% 不透明度），无闪烁。

---

## 14. 实现交接地图（app.js / styles.css：REPLACE vs KEEP）

> 行号基于 `src/mnemoseed/console/static/app.js`（4648 行，2026-08-15 状态）。**REPLACE = 改结构；KEEP = 保留逻辑，仅吃新 token/新渲染壳。**

### 14.1 向导（§10.4）—— REPLACE

| 现状函数 | 行 | 动作 | 说明 |
|---|---|---|---|
| `showDreamSetup()` | 695-727 | **REPLACE** | 移除行 706 对 `oauth-availability` 的并发预拉取；改为只拉 `routes`；新增 `wizard.step=0`、`wizard.connection=null`；Step 0 渲染三张连接卡 + skip。 |
| `renderWizardPanel()` | 729-736 | **REPLACE** | step 0/1/2/3 四态分发；Step 1 按 `connection` 变形。 |
| `wizardStepBar()` | 738-747 | **REPLACE** | 步骤标签改为 `connection → host login / key + model → test & save`（或按连接方式动态）。 |
| `wizardOAuthRows()` | 749-773 | **REPLACE** | 移入 Step 1 ① 分支，且**仅在 ① 选中后**调用；loading 态先行；三态 + 粘贴 token 兜底（§10.4.2）。 |
| `wizardStep1Html()` | 793-808 | **REPLACE** | 拆为 `wizardStep0Html()`（连接卡 + skip，去掉行 798 的 `wizardOAuthRows` 先行渲染）+ `wizardStep1Html()`（按连接方式变形）。 |
| `wizardStep2Html()` | 854-881 | **KEEP**（壳换 token） | key + endpoint + model 变形逻辑不动；Ollama/oauth 隐藏规则沿用。 |
| `wizardStep3Html()` | 883-910 | **KEEP** | 共享复选框 + test/save 逻辑不动。 |
| `wizardPayload/Collect/Test/Save` | 912-1011 | **KEEP** | 后端契约不变；save gate（probe signature）是静默修复的兜底，保留。 |
| 事件 `wz-*`（`handleClick`） | 3497-3545 | **REPLACE** | `wz-next`/`wz-back` 改为按 step 0-3 + connection 分发；新增 `wz-conn`（选连接卡）、`wz-oauth-lazy`（选①后拉 availability）；`wz-skip` 文案换一等可见版本。 |

### 14.2 ⑧ 编辑器（§10.1）—— 逻辑 KEEP，视觉 token

| 现状函数 | 行 | 动作 |
|---|---|---|
| `llmEditFormHtml()` | 3114-3159 | **KEEP**（表单结构、morph 字段、save gate 全部保留） |
| `llmEditorProviderCard()` / `llmEditorOAuthCards()` | 2830 / 2844 | **KEEP**（三态 oauth 卡逻辑正确）；重排：`llmEditorOAuthCards` 作为选中的"host login 卡"呈现，不必置顶 |
| `llmOauthPasteHtml()` / `llmCustomPasteHtml()` | 2872 / 2903 | **KEEP**（粘贴 token/key 双路径保留） |
| `llmApplyEditorProvider()` | 3049-3101 | **KEEP**（morph 规则完整） |
| `llmSyncEditorGate()` | 2951-2969 | **KEEP**（per-route gate：expired/absent 时 Test/Save/Load 禁用 + fix note——静默修复的兜底） |
| `saveRoute()` | 3283-3359 | **KEEP**（probe-signature 门 + 显式 reason，§7） |
| `testRoute()` / `llmProbeMessage()` | 3201 / 666-692 | **KEEP**（大白话探测文案已实现） |
| `oauthHintsHtml()` / `llmRoleCard()` / `llmShellHtml()` | 2757 / 2775 / 2732 | **KEEP**（shell 结构）；只吃新 token |

### 14.3 styles.css（token 化）

| 现状 | 行 | 动作 |
|---|---|---|
| `:root` | 4-25 | **REPLACE** 为 §13 的 token 集（保留旧类名别名） |
| `.wizard-provider-card` + `.selected`/`.muted` | 442-452 | **REPLACE** 样式体为 §13 状态规则；结构类名保留（JS 依赖它们） |
| `.key-teaching` / `.wizard-step-active` / `.wizard-share` | 455-458 | **KEEP** 结构；换 token 色 |
| 新增 `.wizard-conn-card`（Step 0 连接卡）、`.wizard-oauth-loading`（OAuth 探测 loading 占位）、`.skip-firstclass`（一等 skip 行）、`.gate-note`（per-route blocked note） | 新增 | 跟随 §10.4 新增类 |
| 其余组件（`.btn`/`.field`/`.badge`/`.tile`/`.card`） | 全局 | 渐进换 `--ms-*` token（别名先行，逐步收紧） |

---

## 15. Playwright 验收计划（我的后续职责）

> 实现完成后的 UI 验收由我（web-designer）通过 Playwright 完成——**交付前最终验收不再只靠代码评审**。复用仓库内已装的 Playwright（`.bench/graphview-three/node_modules`，v1.62.1；`node` 脚本直接 `require` 该路径）。

### 15.1 运行方式

```
MNEMOSEED_HOME=<临时目录> mnemoseed up --port 18765   # 临时 daemon，绝不动 dogfood 18763
node .bench/llm-ux-check.mjs                          # require(".bench/graphview-three/node_modules/playwright")
```

- 脚本：登录（owner setup → token）→ 依次打开各页 → 断言 → **截图进 `.bench/shots/` 供 JH 审阅**（Step 0、Step 1 ① 三态、Step 2 morph、⑧ 编辑器、blocked-save、窄屏）。
- 窄屏：`viewport { width: 360, height: 800 }` 走一遍向导 + ⑧，断言无横向滚动、卡片可点。

### 15.2 页面与断言清单

| 页面 | 断言（全部可脚本化） |
|---|---|
| 向导 Step 0 | `data-wizard-panel` 存在；**恰好三张连接卡**；**0 个 oauth 行 / 0 个 codex/grok plaque**（`[data-oauth-*]` 计数 = 0）；skip 可见；continue 在选卡前 disabled、选卡后 enabled。 |
| 向导 Step 1 ① | 选①后 `oauth-availability` 被调用（网络日志）；loading 占位 → 三态行；expired/absent 行按钮 disabled + 显示"log in first"文案 + 粘贴 token 兜底可见。 |
| 向导 Step 1/2 ② | 选②后出现服务商卡；**无任何 oauth 控件**（死输入断言）；key + endpoint + model morph 正确（Ollama 无 key 字段）。 |
| 向导 Step 3 | `Test connection` → 失败时 save 保持 disabled 且错误行显示原因（静默修复断言）；通过后 save enabled。 |
| ⑧ 编辑器 | 编辑 deep_reflection：provider 卡单选；oauth 卡仅当选 host login 出现；`data-llm-gate-note` 在 expired/absent 时可见且带修复文案；save 在未探测时 disabled 且点击出原因。 |
| ⑧ 窄屏 360px | 无 `document.documentElement.scrollWidth > innerWidth`；连接卡/服务商卡可点击；无元素溢出。 |
| 全站 token | `getComputedStyle(:root)` 读取 `--ms-bg/--ms-card/--ms-accent/--ms-radius` 均为 §13 值；`--bg` 别名仍解析（兼容）。 |

### 15.3 通过门槛

- 上述断言全绿 **且** 截图中无死输入、无静默 no-op、窄屏无溢出 → 我出验收结论（PASS/FAIL + 截图路径）交 orchestrator。
- **FAIL 打回工程师**（与 QA gate 同纪律），不自己改源码。
