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
| `ollama` | 原生 `POST /api/chat`，无 key；探测 = `GET /api/tags` | 本机 **Ollama**（离线轨） |
| `oauth` | 复用宿主 `~/.codex/auth.json` / `~/.grok/auth.json`，OIDC 刷新 | 仅限 Codex / Grok 宿主登录（`SUPPORTED_PROVIDERS = ("codex", "grok")`） |
| `stub` | 确定性离线桩（仅测试 / 人工评审阶段使用） | 永不该是用户可见的服务商 |

**不存在** Fireworks 或 OpenRouter 驱动，也**无需**新建——两者都由 `openai_compatible` 承担。原生 `anthropic` 驱动存在。**没有**目录接口；目录搭在探测结果上，成功时返回 `detail["models"]`（`openai_compatible.py:88`、`anthropic.py:95`、`ollama.py:78`）。

### 2.2 路由 payload 语义（`admin.py:104-130`）

`GET /api/v1/llm/routes` 每角色返回：`driver`、`model`、`base_url`、`api_key_env`、`provider`——但 `base_url`/`api_key_env`/`provider` **仅在显式设置时**返回（`table.get(...)`），因此生效默认值（`https://api.fireworks.ai/inference/v1`、`MNEMOSEED_DEEP_REFLECTION_API_KEY,FIREWORKS_API_KEY` 回退链）对 console 编辑表单**不可见**。用户打开"编辑路由"，看到空白的 base URL 和空白 key 字段，对正在生效的默认值一无所知。

### 2.3 死输入的具体位置（exact dead-input）

- **向导**（`console/static/app.js` `dreamSetupHtml`，约 474-520 行）：BYOK 表单无论选中哪个驱动，都固定渲染五个字段，包括 `oauth provider`（占位符 `codex | grok`）。选 `openai_compatible` 它照样显示。源码已确认。
- **console ⑧ 编辑表单**（`llmEditFormHtml`，约 2387-2407 行）：`oauth provider` 文本输入框对每个驱动都渲染，包括 openai_compatible / anthropic / ollama。

存在原因：`provider` 是真实的路由参数，OAuth 路径需要它。但 UI 把它当作文本字段暴露给所有流程。正确做法：OAuth 路径是**一条独立的路由选择**，不是通用表单上的一个字段（见 §6）。

### 2.4 术语与心智模型错位

- 向导的驱动下拉列出原始名：`anthropic / oauth / ollama / openai_compatible / stub`。用户按品牌思考，不是按驱动。
- 占位符对每个驱动都是 Anthropic 中心的：model `e.g. claude-opus-5`、key `e.g. ANTHROPIC_API_KEY`。`claude-opus-5` 与配置样例里的 `claude-sonnet-5`（`config.py:376`）都是**未经核实的 model id**——默认值里不要放未核实 id。
- 向导**只**配置 `deep_reflection`（`submitWizard` POST 到 `/api/v1/llm/routes/deep_reflection`，app.js:572）；没有任何角色说明，没有选择。
- 连通性失败暴露原始内部信息：`unreachable — {"error":"GET /models returned HTTP 401"}`（console）、`connectivity test failed: GET /models returned HTTP 401`（CLI `llm set`、`onboard`）。没有任何修复指引。
- `stub` 是向导/console 下拉中的合法选项——把测试驱动摆给了用户。

### 2.5 CLI `onboard` LLM 步骤（`onboard/service.py:202-227`）

提示 `llm driver (e.g. ollama, anthropic, stub)` 与 `llm model`——没有 `base_url`，没有 `api_key_env`。因此 Fireworks/OpenRouter/Anthropic 用户**根本无法**从 CLI 配置云服务商（没有 key 环境变量被采集 → 探测 401 → 步骤静默跳过，显示"connectivity test failed"）。提示的示例驱动全是术语，且漏掉了实际默认驱动。

### 2.6 环境变量时序真相（必须被教会）

`RoleRouter.resolve()` 在**首次物化**时从**守护进程进程环境**读取 key，并缓存实例（`routing.py:56-88`）。UI 必须教会用户的后果：(a) 在*新终端*里设置的环境变量，对*已在运行的*守护进程不可见；(b) Windows 的 `setx` 只对后续新进程生效，不影响运行中的守护进程；(c) 修复方式 = "设置变量，然后重启守护进程"。当前向导的提示——"the daemon reads the key from the named env var at run time"——具有误导性且不完整。

### 2.7 文档 vs 代码漂移（必须解决，而非绕过设计）

| 承诺 | 代码现实 |
|---|---|
| FR-6.9：向导顺序 ① OAuth ② BYOK ③ 离线轨，作为*引导式序列* | 向导把 OAuth 与 BYOK 并排展示；"use X OAuth" 按钮只是预填同一个表单。没有序列化引导，没有"离线轨"呈现。 |
| FR-6.9："中国用户可选 MiniMax/Kimi 等 CLI 服务商，选择时明示数据出境提示" | **未实现。** 没有任何 MiniMax/Kimi 服务商，任何地方都没有出境提示。 |
| FR-6.9："Anthropic 订阅明确不做" | 代码正确——`oauth` 只支持 codex/grok；`anthropic` 仅 key。一致。 |
| design/02 §6：默认 deep_reflection → Kimi K3（Fireworks），short_increment → DeepSeek V4 Flash（Fireworks），local_track → ≤14B Ollama | 与 `DEFAULT_LLM_ROUTES`（`config.py:138-164`）一致。Fireworks model id 已在配置注释中核实；视为可信默认。 |
| PRD-07 G-AC2：⑧ 配置全部三个角色 | 是；**向导**只配置 deep_reflection。有意的但未成文——见 §8 决策 D4。 |

---

## 3. 设计：一个"provider-first 路由配置器"组件

一个组件统领全部三个表面（§10）。它的职责只有一件事：**"我有一个服务商账号——把它配好，让我的梦境能跑起来。"** 它从不同用户索要 driver 名，对当前选择用不到的字段绝不显示，也绝不让任何默认值隐藏。

### 3.1 第一步——服务商选择器（品牌优先，驱动无关）

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

| 卡片 | driver | base_url（预填、可编辑） | key 环境变量（预填、可编辑） |
|---|---|---|---|
| Fireworks | openai_compatible | `https://api.fireworks.ai/inference/v1` | `FIREWORKS_API_KEY` |
| OpenRouter | openai_compatible | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` |
| Anthropic | anthropic | `https://api.anthropic.com` | `ANTHROPIC_API_KEY` |
| Ollama | ollama | `http://localhost:11434` | （无——不需要 key） |
| 其他 | openai_compatible | 空 → 必填 | 默认 `MNEMOSEED_DEEP_REFLECTION_API_KEY` |

**OAuth 路径是卡片上方的一排独立按钮**（仅向导，见 §6），永远不是一张卡片、也永远不是文本字段。

### 3.2 第二步——变形表单

表单体随选择变化（渐进披露）：

- **key 字段只为需要 key 的服务商渲染。** Ollama 时 key 块消失。
- **API key 字段只索要环境变量名**（永不要值——G-AC2 红线），预填该服务商的标准名，下方带一个可折叠的、按操作系统拆分的"如何设置"教学块（§5）。
- **base_url** 预填且可编辑，带一键"重置为 <服务商> 默认"。在引导表面收进"Advanced: endpoint"；在 ⑧ 编辑器完全展开。
- **model** 是组合框：探测结果里的实时目录（§7）+ 精选建议 + 自由输入。Ollama 的目录来自 `GET /api/tags`，模型缺失时给出"先 pull"提示（`ollama pull llama3.1:8b`）。

### 3.3 第三步——角色分配 + 测试 + 保存

向导用一句话说明它要配置的角色（§4），⑧ 编辑器在每个角色卡片上重复这句话。然后：**Test connection** → 通过则**启用 Save**，探测失败则保留表单并显示修复指引（§7）。

---

## 4. 各角色指引（每个角色一句话，常显）

用于 ⑧ 页（角色卡片副标题）与向导（一行说明）。

| 角色 | 一句话说明（UI 字符串） | 推荐搭配 |
|---|---|---|
| deep_reflection | "The careful model. Reads your recent sessions and writes the distilled facts into long-term memory. Use the strongest model you can afford here." | Fireworks kimi-k3（默认）· Anthropic claude-opus 档 · 预算内任何强云模型 |
| short_increment | "The quick model. Handles the frequent small consolidation passes. Use a fast, low-cost model." | Fireworks deepseek-v4-flash-0731（默认）· 快速/低成本的云模型 |
| local_track | "The private model. Runs on this computer, offline, for free. Lower synthesis quality, but nothing leaves the machine." | Ollama + `llama3.1:8b`（默认）——须先 pull |

出现以下术语时必须带 tooltip/展开器：**endpoint**（"服务商接收 MnemoSeed 请求的地址"）、**env var**（"存于你电脑环境里的具名值——MnemoSeed 从中读取 key，它本身绝不存 key"）、**context / max tokens**（"模型单次允许产出的文本量"）、**OpenAI compatible**（"Fireworks 与 OpenRouter 说的同一种 API 方言——一条代码路径即可通吃"）。

---

## 5. API key 教学块（"key 到底放哪"的答案）

渲染在 key 字段下方，所有需要 key 的服务商都显示。布局（console/向导）：

```
API key
[ FIREWORKS_API_KEY        ]  ← env-var name (MnemoSeed stores this name, never the key)

Your key lives in an environment variable. MnemoSeed reads it from there at run time —
you never paste the key into MnemoSeed, and MnemoSeed never stores it.

1. Create a key:  https://app.fireworks.ai/settings/users/api-keys   [open]
2. Set it as an env var, then restart the daemon:

   Windows (Command Prompt):   setx FIREWORKS_API_KEY "your-key"
   Windows (PowerShell):       $env:FIREWORKS_API_KEY = "your-key"    (current window)
                               [setx FIREWORKS_API_KEY "your-key"]     (permanent)
   macOS / Linux:              export FIREWORKS_API_KEY="your-key"     (add to ~/.zshrc)

   Then restart MnemoSeed so it picks the variable up:  [how to restart] → (shows the
   one-line restart command for this machine)
```

行为要点：

- `setx` 是 Windows 的永久形式，但只对**新启动的**进程生效——运行中的守护进程看不到。教学块用大白话直说这一点，并由探测（§7）确认可见性（401 ⇒ 设置并重启）。
- 文案是**按服务商定制**的：key 创建 URL、标准环境变量名、精确命令。macOS 与 Windows 各带自己的命令标签页；Windows GUI 用户永远看不到纯 bash 指令，反之亦然。
- 重启指引每个平台、每种启动方式（autostart vs `mnemoseed up`）各给一行，放进同一个可折叠块。

服务商快速上手事实（已对照官方文档核实；URL 记录在 §12）：

| 服务商 | key 创建 | 环境变量 | base_url | 目录接口 |
|---|---|---|---|---|
| Fireworks | app.fireworks.ai/settings/users/api-keys | `FIREWORKS_API_KEY` | `https://api.fireworks.ai/inference/v1` | `GET /models` |
| OpenRouter | openrouter.ai（账号 → keys） | `OPENROUTER_API_KEY` | `https://openrouter.ai/api/v1` | `GET /api/v1/models` |
| Anthropic | platform.claude.com/settings/keys | `ANTHROPIC_API_KEY` | `https://api.anthropic.com` | `GET /v1/models` |
| Ollama | 无 | 无 | `http://localhost:11434` | `GET /api/tags` |

---

## 6. OAuth 可见性逻辑（消灭死输入）

**规则：OAuth 控件只在 OAuth 路径真正被提供时出现，且 `provider` 值只能由专用按钮设置——绝不来自自由文本字段。**

1. **永不**在任何 BYOK/驱动表单里渲染自由文本的 `oauth provider` 字段。从 `dreamSetupHtml` 与 `llmEditFormHtml` 中移除。
2. **仅向导**——"复用本机已登录"面板渲染在服务商卡片上方，只列出检测到登录的服务商（`oauth-availability`）：
   - `present && !expired` → `[Use Codex login]` 可用。点击后表单切入 **OAuth mode**：driver=oauth、provider=codex、model 字段保留，base_url 与 key 字段隐藏，并显示一条 banner："MnemoSeed will use the Codex login on this machine — no key needed. It refreshes itself while you're signed in."
   - `present && expired` → 行显示"expired — sign in again with the Codex CLI, then come back here"，按钮禁用，附一行重新登录命令。
   - `!present` → 行置灰："no Codex login detected on this machine"。
   - OAuth mode 的 model 字段保留精选建议（`gpt-5.6-codex` 等在发布前必须**核实**——不要发布当前这个未核实占位符）。
3. **console ⑧**——OAuth 状态行（徽章）保持只读。在路由编辑器里，"Reuse Codex login" 是选择器里的一张*服务商卡片*（仅在检测到时），不是文本字段。编辑已有 oauth 路由时在 OAuth mode 打开。
4. **CLI**——`--provider` 仍是 `llm set` 的合法 flag（脚本化对齐），但交互式 `onboard` 向导绝不把它当自由文本问；而是把检测到的登录列为编号选项。

每个字段 × 每个服务商选择的决策表（实现者的单一事实来源）：

| 字段 | openai_compatible（Fireworks/OR/其他） | anthropic | ollama | oauth mode |
|---|---|---|---|---|
| API key 环境变量名 | ✅ 可见，预填 | ✅ 可见，预填 | 隐藏 | 隐藏 |
| base_url | ✅ 可见（高级） | ✅ 可见（高级） | ✅ 可见（高级） | 隐藏 |
| model | ✅ 可见 + 目录 | ✅ 可见 + 目录 | ✅ 可见 + 目录 | ✅ 可见（建议） |
| oauth provider 文本字段 | **绝不** | **绝不** | **绝不** | **绝不**（仅按钮设置） |
| max tokens（仅 ⑧） | ✅ 高级 | ✅ 高级 | 隐藏 | ✅ 高级 |

---

## 7. 探测 / 测试 UX（大白话，先给修复）

### 7.1 状态与文案

| 探测结果 | 呈现（UI 字符串） |
|---|---|
| 进行中 | `Testing connection to Fireworks…`，标准 loading 样式，按钮禁用 |
| 成功 | `Connected to Fireworks — key in FIREWORKS_API_KEY works. Found 1,204 models.`（绿色）。model 下拉从 `detail.models` 填充；**Save route** 武装。 |
| 401 / 403 | `Fireworks rejected the key in FIREWORKS_API_KEY. It's missing, wrong, or expired — check it at <provider key URL>, set it again, then restart MnemoSeed.` |
| 连接被拒 / DNS（Ollama） | `Can't reach Ollama at http://localhost:11434. Is the Ollama app running? Install from ollama.com, then pull a model (ollama pull llama3.1:8b).` |
| 连接被拒 / DNS（云） | `Couldn't reach <provider>. Check your internet connection or firewall, then try again.` |
| 超时 | `Timed out talking to <provider>. The endpoint may be slow or blocked — check <endpoint> and try again.` |
| 未知 driver（UI 下不应出现） | `That connection type isn't built in — go back and pick a provider.` |
| 未通过探测就保存被拦 | `Test the connection first — a route can only be saved after it works.` |

### 7.2 行为

- 探测失败**保留全部字段**；什么都不丢。修复块内联、聚焦，精确指向要改的字段。
- 401 情况复用 §5 的 key 教学块（折叠），带"重启守护进程"动作，让用户无需重新输入任何东西即可修复。
- 成功时刷新目录：model 组合框从探测结果的 `models` 列表重新填充（无需后端改动——它已经搭在 `detail["models"]` 上）。更干净的长期方案见 D2。
- 旧的原始渲染（`reachable — {"error":...}` JSON、"unreachable" 徽章）处处替换，包括 ⑧ 角色卡片探测徽章 → 改为 `connected` / `needs attention`，卡片上显示同一条大白话消息。

---

## 8. 需要拍板的决策（orchestrator / 产品）

> D1 已定案：**选 Option B**——设置入库为主（settings DB 为 primary）+ 热生效（hot-apply）+ 预留 scope 列；key 仍全部来自环境变量，绝不入库；SaaS key 托管推迟到 TEE 里程碑。

| # | 问题 | 选项 | 建议 |
|---|---|---|---|
| D1 | key 处理：纯环境变量（现状）vs"粘贴 key 由 MnemoSeed 写入用户环境变量 / 系统凭据库"？ | (a) 纯环境变量 + 教学（现状，G-AC2 干净）；(b) 从 console 写一个 `~/.mnemoseed/.env` 或 OS keychain 条目；(c) 完整 OS 凭据库集成 | **已定案**——设置 DB 为主 + 热生效，key 仍 env 来源、不入库；见上注。 |
| D2 | 实时模型目录：复用探测 `detail["models"]`（后端零改动）vs 新建 `GET /api/v1/llm/catalog?driver=&base_url=` 接口？ | (a) 仅探测；(b) 专用目录接口 | **(a) 本轮**——先交付 UX；(b) 作为发布打磨后续（探测是按需的，首次访问在用户测试前看不到目录——happy path 可接受）。 |
| D3 | 原生驱动：无需新建——Fireworks/OpenRouter = openai_compatible，Anthropic 原生，Ollama 原生。确认？ | — | **确认；无需驱动工作。** |
| D4 | 向导角色范围：仅 deep_reflection（现状）vs "同时用于 short_increment" 复选框（写两个角色）vs 让向导配置全部三个？ | (a) 现状；(b) +共享复选框；(c) 完整三角色向导 | **(b)**——一个复选框、一行文案，覆盖常见的"一把 key、一个服务商"用户，又不把 TTFM 拖过 3 分钟。local_track 永远保持 Ollama 离线。 |
| D5 | 把 `stub` 驱动从向导/console 下拉隐藏（保留在 API 与配置里供测试）？ | (a) 隐藏；(b) 保留 | **(a) 隐藏**——测试接缝不是用户路径。 |
| D6 | MiniMax/Kimi 出境路径（FR-6.9）：实现，还是删掉承诺？ | (a) 在"Other OpenAI-compatible"卡片上加"中国区域"说明，附数据出境提示；(b) 实现前从文档删除 | **(a)**——零代码、一条提示，补回一个已文档化的承诺；提示写明"记忆会出境到服务商服务器"。 |
| D7 | `onboard` CLI LLM 步骤：扩展为采集 base_url + api_key_env + 服务商选择？ | (a) 是，镜像组件；(b) 保持 driver+model | **(a)**——不这样，今天 CLI 根本无法配置云服务商（见 §2.5）。 |
| D8 | 探测错误分类：前端解析字符串（现状）vs 后端新建结构化 `error.kind`？ | (a) 前端解析；(b) 后端 kinds | **(a) 现在，(b) 以后**——§7.1 的三四种错误类稳定，与现有字符串匹配。 |
| D9 | 核实 model id：当前占位符/`default_config_toml` 样例（`claude-opus-5`、`claude-sonnet-5`）未核实。 | (a) 只从目录取，不发布未核实 id；(b) 对照服务商文档核实 | **(a)+(b)**：发布时用目录核实过的 id 替换未核实 id；Fireworks 默认值（配置注释已核实）维持不变。 |

---

## 9. 空态 / 错误态 / 加载态 + 无障碍

### 9.1 各表面状态

| 状态 | 行为 |
|---|---|
| 加载（向导/⑧） | 沿用现有 `Loading…` 骨架，带角色/服务商占位；绝不让页面空白。 |
| `oauth-availability` 拉取失败 | OAuth 面板隐藏，服务商卡片 + BYOK 仍可用（`showDreamSetup` 的 catch 已如此）。 |
| 未检测到任何服务商（向导） | OAuth 面板显示一行置灰文字："No Codex/Grok login detected on this machine — you can still use an API key below." |
| 探测成功但目录为空 | model 组合框回退到精选建议 + 自由输入；提示 "The catalog returned no models — pick from the suggestions or type the exact model id." |
| daemon 宕机 / 拉取失败（⑧） | 沿用现有错误面板 + Retry，不变。 |
| 保存 → 409（test-required 竞态） | 映射为大白话 "Test the connection first"，绝不给原始 409 详情。 |
| 路由卡片无显式配置（⑧） | 用 "defaults" 徽章代替空块——生效 base URL / key 链 / model 现在在卡片上可见，而非只有编辑时可见。 |

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
│  it. Key values never appear here — only env-var names.               │
│                                                                       │
│  host logins: [codex: logged in] [grok: not detected]                 │
│                                                                       │
│  ┌─ deep_reflection ── the careful model ──────────────────────────┐  │
│  │  connected · Fireworks · accounts/fireworks/models/kimi-k3      │  │
│  │  key: MNEMOSEED_DEEP_REFLECTION_API_KEY → FIREWORKS_API_KEY     │  │
│  │  base URL: https://api.fireworks.ai/inference/v1  ·  max 2048   │  │
│  │  [test connection] [edit route]                                 │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│  ┌─ short_increment ── the quick model ── ... (same card) ─────────┐  │
│  ┌─ local_track ── the private model ── ollama · llama3.1:8b ──────┐  │
│                                                                       │
│  [edit route] expands the provider-first form (§3) inline:           │
│    provider picker → morphing form → test → save (armed only after   │
│    a passing probe of the exact values)                              │
└───────────────────────────────────────────────────────────────────────┘
```

### 10.2 首次运行向导（owner 创建后）

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
│  [Codex: logged in]  → [Use Codex login]      (expired/absent → fix │
│  copy, never a dead button)                                          │
│                                                                      │
│  [continue]                                            [skip for now]│
└───────────────────────────────────────────────────────────────────────┘
```
第二步按服务商变形（§5），第三步跑探测（§7）然后保存 deep_reflection（+ 按 D4 的 short_increment）。"skip for now" 保持 capture-only daemon——明说，而非让用户自行发现。

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
  Set it as an env var and restart MnemoSeed:
    Windows:  setx FIREWORKS_API_KEY "your-key"
    macOS/Linux: export FIREWORKS_API_KEY="your-key"   # add to ~/.zshrc
  api key env var [FIREWORKS_API_KEY]:
  model [accounts/fireworks/models/kimi-k3]:            ← verified default
  testing connection to Fireworks…
  connected — key works. saving…
  ✓ dream model configured (openai_compatible/accounts/fireworks/models/kimi-k3)
  (skip: entering no model → "capture-only daemon (dreaming disabled)"; 
   --skip llm and --llm-driver/--llm-model scripted flags unchanged)
```

CLI 与 console **共享同一套**后端（先 `POST /api/v1/llm/test`，再 `/api/v1/llm/routes/deep_reflection`）、同一张服务商表、同一条大白话探测文案、同一个默认值出处。术语在首次出现时内联定义（"env var = 这台电脑上一个具名值，MnemoSeed 从它读 key"）。

---

## UI 文案（English —— 产品界面用）

以下为产品 UI 字符串资产，保持英文原文，逐字保留。

### 11.1 向导

- Title: `dream model`
- Intro: `Pick the model that distills your sessions into long-term memory. One model gets
  you started — you can change any role later in Models.`
- Provider group: `Which provider do you use?`
- Fireworks card: label `Fireworks`, blurb `Recommended starting point — MnemoSeed's default
  models run here.`
- OpenRouter card: label `OpenRouter`, blurb `One API key, hundreds of models from many labs.`
- Anthropic card: label `Anthropic (Claude)`, blurb `Requires an Anthropic API key from
  platform.claude.com.`
- Ollama card: label `Ollama on this computer`, blurb `Free and offline. Runs entirely on
  this machine; lower synthesis quality.`
- Other card: label `Another OpenAI-compatible API`, blurb `Point at any other endpoint
  that speaks the OpenAI chat API.`
- OAuth panel header: `Or reuse a login already on this computer`
- OAuth hint: `MnemoSeed uses that login's access — you don't paste a key. No key value is
  read, sent, or stored.`
- OAuth live: `Codex login found — sign in is current.` / button `Use Codex login`
- OAuth expired: `Codex login found but expired — sign in again with the Codex CLI, then
  return here.` (button disabled)
- OAuth absent: `No Codex login detected on this machine.` (muted)
- OAuth banner (after selection): `Using the Codex login on this machine — no key needed.
  It refreshes itself while you're signed in.`
- Key label: `api key env var`
- Key teaching intro: `Your key lives in an environment variable. MnemoSeed reads it from
  there — you never paste the key here and it is never stored.`
- Key 401 fix: `Fireworks rejected the key in FIREWORKS_API_KEY — it's missing, wrong, or
  expired. Set it again, then restart MnemoSeed.` (per-provider substitution)
- Endpoint label: `endpoint` / advanced header: `Advanced: endpoint`
- Endpoint reset: `reset to Fireworks default`
- Model label: `model`
- Model placeholder: `type or pick a model`
- Catalog empty: `No models listed — pick a suggestion or type the exact model id.`
- Probe in-flight: `Testing connection to Fireworks…`
- Probe ok: `Connected — key in FIREWORKS_API_KEY works.`
- Probe saved: `dream model configured: deep reflection → <model>`
- Skip button: `Skip for now — capture-only (dreaming stays off)`
- Skip confirm: `Skipped — MnemoSeed keeps capturing sessions, dreaming stays off until a
  model is configured. You can set one any time in Models.`

### 11.2 console ⑧

- Page title: `models & routing`
- Page note: `What each role does, and which model serves it. Key values never appear here —
  only the env-var names MnemoSeed reads them from.`
- Role subtitles (§4).
- Card probe: `connected` / `needs attention` (with the plain message from §7, not raw JSON)
- Card key line: `key: MNEMOSEED_DEEP_REFLECTION_API_KEY → FIREWORKS_API_KEY`
- Card base: `base URL: https://api.fireworks.ai/inference/v1`
- Defaults chip (no explicit config): `defaults`
- Buttons: `Test connection` / `Edit route` / `Cancel edit` / `Save route` (disabled until a
  passing probe of the exact values)
- Save gate error: `Test the connection first — a route can only be saved after a passing
  probe of these exact values.`
- 409 mapped: same as save gate error.
- Editor header: `Edit route — deep_reflection`
- Provider group in editor: `Which provider?` (same cards, minus "recommended")
- max tokens label: `max tokens` (advanced), note: `blank = role default`
- Saved banner: `route deep_reflection saved — config version <v> (audited)`
- Restart note (first time a new env var is introduced): `Remember: the daemon reads env
  vars from its own startup environment. If you set a new one, restart MnemoSeed.`
- OAuth line: `host logins: codex — logged in · grok — not detected`

### 11.3 CLI（onboard LLM 步骤 + llm set）

- Step header: `[llm]`
- Intro: `Pick the model that distills your sessions into long-term memory. One model gets
  you started; change any role later with 'mnemoseed llm set'.`
- Provider prompt: `provider [1]: ` (list printed as §10.3)
- Key URL line: `Create a key at <url>`
- Env-var teaching (printed once, §10.3 block)
- `api key env var [FIREWORKS_API_KEY]: `
- `model [accounts/fireworks/models/kimi-k3]: `
- `testing connection to Fireworks…`
- `connected — key works. saving…`
- Success: `✓ dream model configured (<driver>/<model>)`
- Fail 401: `error: Fireworks rejected the key in FIREWORKS_API_KEY — set it and restart
  the daemon, then re-run onboard (it resumes here).`
- Ollama fail: `error: can't reach Ollama at http://localhost:11434 — is it running?
  Install from ollama.com and pull a model (ollama pull llama3.1:8b).`
- Skip: `skipping the LLM wizard: the daemon stays capture-only (dreaming disabled until a
  model is configured)`
- `mnemoseed llm set --help`: driver help updated to `provider (or --provider codex|grok
  for a host login)`; add `--provider-card`? No — keep parity, add examples in help text.

---

## 12. 服务商事实核实（存档）

- **Fireworks**：quickstart（key 创建 URL `app.fireworks.ai/settings/users/api-keys`、`setx`/`export FIREWORKS_API_KEY`、OpenAI 兼容 base `https://api.fireworks.ai/inference/v1`、OpenAI SDK 路径隐含 `GET /models`）—— https://docs.fireworks.ai/getting-started/quickstart
- **OpenRouter**：quickstart（base `https://openrouter.ai/api/v1`、`OPENROUTER_API_KEY`、目录 `GET /api/v1/models`、OpenAI 兼容）—— https://openrouter.ai/docs/quickstart
- **Anthropic**：API overview（base `https://api.anthropic.com`、Messages `POST /v1/messages`、`x-api-key` + `anthropic-version`、key 来自 `platform.claude.com/settings/keys`、models `GET /v1/models`）—— https://platform.claude.com/docs/en/api/overview
- **Ollama**：API reference（`POST /api/chat` stream=false、`GET /api/tags`、无鉴权、`model:tag` 命名）—— https://github.com/ollama/ollama/blob/main/docs/api.md

### 12.1 核实说明（本规格的落地依据）

- 代码阅读：`console/static/app.js`（向导 + ⑧ 渲染/编辑/探测，行号已在文中内联引用）、`config.py` `DEFAULT_LLM_ROUTES`、`llm/admin.py` + `admin_routes.py`（仅显式 payload、探测签名、409 门槛化保存）、`llm/routing.py`（env 解析 + 实例缓存）、`llm/drivers/*`（五个驱动，目录在探测 detail 中）、`configwrite/service.py`（env 名校验）、`onboard/service.py`（LLM 步骤）、`cli.py`（`llm status/set`、`onboard`）。
- 本机实测：用临时 `MNEMOSEED_HOME` 在备用端口（嵌入式预设，`127.0.0.1:18764`）启动 daemon，走通：owner 设置 → 登录 → `/api/v1/llm/routes`（确认仅显式 payload）→ `/api/v1/llm/oauth-availability`（本机两个登录都已检测到但过期）→ `/api/v1/llm/test` 探测形态（无 key Fireworks 401；离线 Ollama 连接被拒；未知 driver；未探测直接保存 → 409）。在驱动目录里观察到 `stub`，并在线上 payload 中确认默认值不可见。
- 未核实：当前 model 占位符 `claude-opus-5` / 配置样例 `claude-sonnet-5`（D9），以及 OAuth mode 的 `gpt-5.6-codex` 占位符。
