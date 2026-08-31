# Provider Registry UX（provider 注册表 · console ⑧ 双窗格 / 首次运行向导 / CLI onboard）

> 中文为规范版（canonical）。本文是 **draft**，未定案前不建 EN mirror；定案后按 change set 同步。
> 状态：2026-08-15 · docs-only spec round · 无代码改动。

## 1. 问题陈述

用户的原话（转述，两次）：

> "opencode 对于接入 providers API 的分类管理就很好，除了主流几个 providers，其他 3rd party providers 都可以各自接入 API，然后可以自由选择要使用的 model"

> "选择了 provider，再选择要用 subscription 的方式或 api 登入，清楚多了"

即他想要 **opencode 风格的 provider 连接管理**：

1. **连接（connection）是头等实体**——一个命名连接 = base_url + key 材料（或宿主登录），**接入一次**；
2. **主流 + 任意第三方 OpenAI 兼容端点**都能各自接入（Fireworks / OpenRouter / Anthropic / Ollama / opencode Zen 之外，还能加任意自定义端点、codex/grok 宿主登录条目）；
3. **角色从注册表里选 provider**，而不是每个角色重复录入 provider+key；
4. **连接层就显式区分两条路**：subscription（宿主登录，subscription/host-login）vs API（BYOK）——选择发生在连接这一级，不是埋在表单字段里。

现状的反面（详见 §2）：⑧ 编辑器是**角色中心**的，每个角色打开编辑器都要重新选服务商卡、重填/复选 endpoint 与 key 来源；`LLM_PROVIDERS` 是前端硬编码常量，没有"用户自定义连接"的持久化位置；连接类型（订阅/API/本地）没有被提升为连接层的一等属性。

本规格把 ⑧ 页重构为**双窗格**（Providers 注册表 + Roles 分配），三个表面（⑧ 编辑器 / 向导 / CLI onboard）共享同一个注册表心智与同一套后端。

---

## 2. 落地依据（代码现状，逐条核实）

> 行号以 2026-08-15 状态为准。全部经过源码核对，无杜撰端点。

### 2.1 现状编辑器是角色中心的（每角色重复录入）

`console/static/app.js` 的 `llmEditFormHtml()`（行 3170-3214）是**角色表单**：`Edit route — <role>`，内含 provider 卡单选、model、endpoint（`base_url`）、key 字段（`api_key_env`）、max tokens。保存 `saveRoute()`（行 3338-3414）把字段逐键写进 `dream.llm.<role>.<field>`（driver/model/base_url/api_key_env/max_tokens/provider）。**base_url 与 key 来源是每个角色一份**——两个角色用同一服务商时，用户要在两个角色编辑器里各选一次、各填一次，没有任何"共享连接"的抽象。

`provider` 字段现状写的是**卡片 id**（`fireworks`/`openrouter`/`anthropic`/`opencode`）或 oauth 的 `codex|grok`（`saveRoute` 行 3350 取 radio 值去掉 `oauth:` 前缀；`llmActiveProviderId` 行 3079-3096 反向匹配）。自定义端点（"other" 卡）**不写 provider**——`llm/admin.py:493` 与 `app.js:3384-3388` 显式丢弃 `"other"`，自定义连接靠 `base_url` 反推。也就是说，**现状没有"命名连接"这个持久化实体**。

### 2.2 `LLM_PROVIDERS` 是前端硬编码常量（无自定义、无持久化）

`app.js:476-584` 六个卡：Fireworks / OpenRouter / Anthropic / opencode Zen / Ollama / Another OpenAI-compatible API。无用户新增、无后端对应存储；"other" 卡每次选都要重填 endpoint + key 名。这就是"其他 3rd party providers 无法各自接入"的根因。

### 2.3 连接类型不是连接层一等属性

向导 Step 0（`模型路由配置-UX.md` §10.4，已在 2026-08-15 修复）已经是"连接方式先行"三张卡（① Host login ② API key ③ Ollama local）。但 ⑧ 编辑器没有这一级选择——它把"用宿主登录"渲染成 provider 卡区上方的 OAuth 卡（`llmEditorOAuthCards`，`app.js:2889-2910`），把"订阅 vs API"混进表单内部。用户第二条诉求（"选择 provider，再选 subscription 或 api 登入"）对应：**连接类型必须出现在添加/编辑连接的一级表单里**，且订阅路径在连接层可见。

### 2.4 secrets 引用按角色命名空间（provider 级引用尚不存在）

`secrets/refs.py` 语法 `secrets:mnemoseed/dream/([a-z][a-z0-9_]*)`；config 加载 `config.py:_validate_api_key_ref`（行 299-321）与 configwrite 校验 `configwrite/service.py:_validate_env_name_list`（行 160-188）都把引用的命名段**限定为 live 角色**。`SecretStore` 端口本身是 name 寻址、通用（`secrets/store.py`：`get/set/delete/exists/masked_tail`），`routing.py:resolve()` 对任意 `secrets:` 引用通用解析——**缺的只是语法 + 校验层允许 `providers/<id>` 命名空间**（后端工作，§12.3）。角色级引用 `secrets:mnemoseed/dream/<role>` 保留有效。

### 2.5 探测门槛与 effective 语义（本规格不设计掉，保留）

- 探测门：`llm/admin.py` `set_role` 只在同签名（driver+model+base_url+api_key_env+provider）的探测在宽限窗内通过后才落盘（行 243-257，`LLMTestRequiredError` → 409）；`test_config`（行 365-454）解析 effective key 源探测。**本规格保留**：角色绑定 provider 的保存仍是"对该角色 effective 路由的精确签名探测通过后才可保存"。
- effective 语义：`routes()`（`admin.py:115-164`）顶层字段显式-only、`effective` 携带合并默认；`configwrite` 是唯一写路径（surgical TOML + versioned record + audit + 热应用，`configwrite/service.py`）。**本规格保留**：绑定 provider 后，`effective` 语义扩展为"角色显式 > provider 条目 > 驱动默认"（§12.4），角色独立指向、云+本地混搭规则不变（已定案 D10）。

### 2.6 向导 Step 0 已是连接方式先行（映射到同一注册表）

`showDreamSetup()` / `wizardStep0/1/2/3Html()` 已在（`app.js`，`模型路由配置-UX.md` §10.4）。Step 0 三张卡选择连接方式；选①后延迟探测 `oauth-availability`。本规格把这三张卡接到**注册表**：选卡 = 注册一个对应类型的 provider，再进角色绑定——与 ⑧ 完全同后端（§9.4）。

### 2.7 已定案、本规格不开的重

- **角色模型**：只有 `deep_reflection` / `short_increment` 两个角色；`local_track` 已弃用。（`config.py:168-178`，D10）
- **权限**：模型路由系统级、owner/admin 级（D11；`07-管理控制台.md` §8 页脚文案）。
- **SecretStore 文件后端**：粘贴一次、本地落盘、掩码尾缀、免重启（D1）。

---

## 3. 目标 IA：⑧ 页双窗格

⑧ Models & Routing 从"两长条角色卡 + 内联编辑器"重构为**左右两个面板**：

```
┌─ models & routing ────────────────────────────────────────────────────┐
│  page note + derived offline badge + permission footnote (不变)          │
│  ┌──────────────┬───────────────────────────────────────────────────┐  │
│  │  PROVIDERS   │  ROLES                                            │  │
│  │  (registry)  │  deep_reflection                                  │  │
│  │  · Fireworks │  ┌ pick a provider ────────────────┐              │  │
│  │  · OpenRouter│  │ ◎ Fireworks   ○ OpenRouter      │              │  │
│  │  · Anthropic │  │ ○ Custom      ○ Codex login     │              │  │
│  │  · Ollama    │  └─────────────────────────────────┘              │  │
│  │  · opencode  │  → model (provider catalog) → max_tokens → test → save│
│  │  · Custom A  │  short_increment                                   │  │
│  │  · Codex     │  (same: pick provider → model → max_tokens → save) │  │
│  │  [+ add]     │                                                   │  │
│  └──────────────┴───────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────┘
```

- **Providers（注册表面板）**：命名连接清单——内建（Fireworks / OpenRouter / Anthropic / Ollama / opencode Zen）+ 用户新增自定义条目 + 宿主登录条目（Codex/Grok）。每行：品牌/图标 + 名称 + base_url + 连接类型 + key 状态 chip + `Test` / `Edit` / `Delete`。
- **Roles（分配面板）**：两角色卡。选角色后只展开：**从注册表选 provider → model（provider 的 live catalog）→ max_tokens → Test → Save**。用户**永不**为共享 provider 重填 base_url 或 key。

### 3.1 共享心智模型："一次接入，处处引用"

三个表面（⑧ 编辑器 / 向导 / CLI）都按同一个模型：

```
连接（provider 条目）= 命名 + 连接类型 + 端点 + key 保管/宿主登录   ← 接入一次
角色（role）        = 绑定某个 provider + model + max_tokens        ← 引用
```

---

## 4. provider 注册表模型

### 4.1 注册表条目字段

| 字段 | 说明 | 例子 |
|---|---|---|
| `id` | 唯一 id（自定义生成，如 `custom-xxxx`；内建用固定 id） | `fireworks` / `custom-3f9a` |
| `name` | 显示名（用户可改） | `Fireworks` / `My company gateway` |
| `kind` | `builtin` \| `custom` | 内建 vs 用户新增 |
| `type` | 连接类型（一等属性）：`api-key` \| `host-login` \| `local-none` | §5 |
| `driver` | 由连接类型 + 服务商决定的驱动 | `openai_compatible` / `anthropic` / `ollama` / `oauth` |
| `base_url` | 端点（内建预填、可覆盖；local-none 固定 localhost:11434） | `https://api.fireworks.ai/inference/v1` |
| `key_source` | 命名引用（`secrets:mnemoseed/providers/<id>` 或 env 链）——**值永不在 payload** | `secrets:mnemoseed/providers/fireworks` |
| `provider` | 仅 host-login 条目：`codex` \| `grok` | `codex` |
| `models` | 最近一次探测/目录拉取的 live catalog（只读派生，非存储字段） | `[...]` |

**内建条目永不删除**；可编辑项只允许 `name` 与 `base_url` 覆盖 + key 保管（§14 A6）。自定义条目全字段可编辑、可删除。

### 4.2 内建（built-in）条目

seed 于后端（对应 `LLM_PROVIDERS` 六卡，`app.js:476-584` 已核实的事实与 id）：

| id | name | type | driver | base_url | key 来源 |
|---|---|---|---|---|---|
| `fireworks` | Fireworks | api-key | openai_compatible | `https://api.fireworks.ai/inference/v1` | `FIREWORKS_API_KEY` |
| `openrouter` | OpenRouter | api-key | openai_compatible | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` |
| `anthropic` | Anthropic (Claude) | api-key | anthropic | `https://api.anthropic.com` | `ANTHROPIC_API_KEY` |
| `opencode` | opencode Zen | api-key | openai_compatible | `https://opencode.ai/zen/v1` | `OPENCODE_ZEN_API_KEY`（或粘贴复用 opencode 本机登录，见 §4.4 注） |
| `ollama` | Ollama on this computer | local-none | ollama | `http://localhost:11434` | 无 |
| `codex` / `grok` | Codex / Grok（宿主登录） | host-login | oauth | 无（宿主 CLI 管理） | `~/.codex/auth.json` / `~/.grok/auth.json`（从不读 token 值） |

宿主登录条目按 `oauth-availability`（`admin.py:166-188`）三态渲染，**不会在用户操作前预渲染**（延续 §10.4 修复）。opencode Zen 的零复制复用（读 `~/.local/share/opencode/auth.json` 的 `opencode-go` 条目）仍是文档化 + 粘贴一次路径；宿主登录后端接入留给工程师（现状 leftover，`模型路由配置-UX.md` §12.1）。

### 4.3 自定义条目

`[+ Add provider]` 创建：`name` + 连接类型 +（api-key 时）`base_url` + key。任意 OpenAI 兼容端点为默认（driver=`openai_compatible`）。连接类型可切换至 `local-none`（本地端点，如内部推理网关）或 `host-login`（仅 codex/grok，见 §5）。

### 4.4 key 保管：provider 级 secret 引用

粘贴一次 → 写入 SecretStore 名字 `mnemoseed/providers/<id>` → 配置存 `secrets:mnemoseed/providers/<id>` → 掩码尾缀 `****1234`，可一键删除，删除后回退 env 链。**key 值不进 config、不进 API 响应、不进审计**（与现有 `llm/key` 纪律一致，`admin.py:296-355`）。同一 provider 被两个角色共享 = **同一个 secret 引用**，两份角色配置引用同一 key，值只存一份。

> 注：key 端点 `llm/key` 现为 loopback-only（`admin_routes.py:66-76`）；provider 级 key 端点同纪律。

### 4.5 连接类型（一等属性）

```
connection type   —— 添加/编辑 provider 的第一屏、第一个问题
  ◉ API key        BYOK · paste once → stored locally · masked tail ****1234
  ○ Subscription   Host login on this computer (Codex / Grok) — no key to manage
  ○ Local          No key at all — local endpoint (e.g. Ollama / an internal gateway)
```

`api-key` 在 API/BYOK 路径下选项收窄为 `api-key` 或 `host-login`（仅 codex/grok，`SUPPORTED_PROVIDERS`，`llm/drivers/oauth.py`）；`local-none` 仅本地端点。类型决定后续表单变形（§9.1）。**订阅路径不再是表单里的隐藏分支**——它是连接类型之一，选它就明确进入 host-login 三态（live/expired/absent，`codex login` / `grok login` 命令 + 粘贴 token 兜底，`LLM_OAUTH_LOGIN_CMD`/`LLM_OAUTH_TOKEN_DOCS` 复用）。

---

## 5. Roles 面板：角色只选 provider

`deep_reflection` / `short_increment` 两卡（副标题复用 §4 一句话说明）。点 `Configure`（或 `Edit`）展开角色分配编辑器，**只出现**：

```
┌─ configure deep_reflection ─ the careful model ──────────────────┐
│  which provider?   （来自注册表，单选卡）                           │
│    ◎ Fireworks     ○ OpenRouter     ○ Anthropic     ○ Ollama     │
│    ○ opencode Zen  ○ My company gateway   ○ Codex (subscription) │
│  [use another provider → go to Providers · + add]                 │
│  model [ type or pick from Fireworks catalog            ] ▾       │
│  max tokens [ 2048 ]   (blank = role default)                     │
│  [test connection]   ✓ connected — key works.  [save route]       │
└───────────────────────────────────────────────────────────────────┘
```

- **没有 base_url 字段、没有 key 字段**。两个字段都来自绑定的 provider 条目。
- model 组合框 = provider 的 curated + live catalog（来自该 provider 的探测/目录，§12.1）；`Load model list` 按钮保留。
- `Test connection` 探测的是**该角色 effective 路由**（provider base_url + role model + provider key），通过才武装 `Save`——探测门槛不变（§2.5）。
- 绑定写入：角色的 `provider` 字段 = 注册表 id（复用现有字段，§14 A7）。两个角色绑同一 provider = 共享 base_url + key，互不干扰地各选 model。

---

## 6. 权限与 SaaS AdminPlane

注册表是**系统级**的，本地自托管 = owner 账号（开源单用户构建的唯一账号）；商业多用户 license = admin 级、作用于所有用户；SaaS = 云 Admin Plane（系统操作员级）提供同一套 providers/roles 表面。**不是用户个人设置**（延续 D11）。⑧ 页页脚文案不变，普通用户整页只读。注册表写入（provider CRUD、key）loopback-only + identity gate，与现状一致。

---

## 7. 向后兼容：既有 `[dream.llm.<role>]` 配置

**迁移（mirror-rebuild 路径，一段话）：** 升级后首次加载时，前端把每个角色的 effective 路由**镜像**成注册表视图模型，**不改动、不删除**既有 TOML：已知 driver + 默认 base_url → 选中对应内建卡；自定义 base_url → 合成一个 `Custom — <host>` 注册表条目（仅在用户保存时才持久化为真条目）；effective `api_key_env`（env 名或 `secrets:mnemoseed/dream/<role>` 引用）作为该 provider 的 key 来源展示；角色的 `provider` 字段指向该注册表 id（下一次保存该角色时经正常探测门写入）。用户的 deep_reflection 自定义 modal 路由因此**呈现为"一个注册表条目 + 一个角色绑定"**，零重录；在用户编辑该角色之前，旧表原样存活。保存动作把数据规范化为 registry+binding。

---

## 8. 流程

### 8.1 添加/编辑 provider

```
[+ Add provider] 或 [Edit] 某条 →
  1. connection type:  ◉ API key  ○ Subscription (host login)  ○ Local
  2. (api-key) name · endpoint (prefilled for built-ins, reset-to-default) ·
     key: paste once → stored (****1234) | env var name (headless/CI)
     (subscription) 选中 Codex / Grok → 三态可用性 → Use login / 粘贴 token
     (local)        name · endpoint (default localhost:11434)
  3. [test connection] → ok: "connected — found N models" → [save provider]
```

内建条目编辑只开放 name/base_url + key 保管；类型不可改（`fireworks` 恒为 api-key）——防止把 `fireworks` 改成 host-login 造成语义混乱（§14 A6）。

### 8.2 跨角色共享

角色 A 绑定 `fireworks` 后，角色 B 打开 `Configure` 直接看到 `◎ Fireworks` 卡（同一注册表条目），选它 + 选 model + test + save。key 不再二次录入。**任一角色改绑或删除 provider 不影响另一角色的绑定**（各自独立引用，`RoleRouter` 按角色热应用，`routing.py` generation 缓存）。

### 8.3 删除 provider（ask-warning + 安全回退）

```
Delete "My company gateway"?
  → 注册表检查：哪些角色绑定它。
  → 若被引用：⚠ "2 roles use this connection: deep_reflection,
               short_increment. Deleting it removes them from those roles;
               each falls back to its previous route (defaults, or the
               route you had before this connection)."
     [cancel] [delete anyway]
  → 确认后：后端在同一次写入中清空绑定角色的 provider 引用 + 删除条目；
     角色按既有显式字段/默认路由解析（RoleRouter 懒物化，坏路由不破启动，
     `routing.py:84-90`），角色卡显示 `defaults` / `needs attention`。
```

**内建条目没有 Delete 按钮**（只可编辑 name/base_url + key）。

### 8.4 向导版本（同一后端）

向导 Step 0 三张连接卡 = **注册**一个 provider：

| Step 0 卡 | 注册动作 | 后续 |
|---|---|---|
| ① Use a login on this computer | 注册 host-login 条目（codex/grok，三态） | Step 1 选宿主 → Step 2 model → Step 3 test & save |
| ② Bring your own API key | 注册 api-key 条目（Fireworks/OpenRouter/Anthropic/其他/opencode Zen） | Step 1 选服务商 → Step 2 key+model → Step 3 test & save |
| ③ Run locally | 注册 local-none 条目（Ollama） | 直达 Step 2 model + 质量提示 → test & save |

保存走 `/api/v1/providers` + 既有 `/api/v1/llm/routes/<role>`（探测门不变），Step 3 的 `also apply to short_increment` 共享复选框保留（D4）。CLI onboard LLM 步骤镜像同样流程（编号菜单，无单选 UI）。

---

## 9. 状态与文案（English copy deck —— UI 字符串资产，逐字保留）

### 9.1 页级（⑧）

- Page title: `models & routing`
- Page note: `Connections live in the provider registry. Each role picks a connection and a model — you never enter an endpoint or key twice. Key values never appear here — only a masked tail (****1234).`
- Providers pane header: `providers` · `Add provider` · empty: `No custom providers yet — add any OpenAI-compatible endpoint.`
- Roles pane header: `roles` · `each role picks a provider + model`
- Permission footnote: `Model routing is system-scoped — set by the owner/admin and applies to every user.`（不变）
- Offline badge（派生，不变）: `fully offline — nothing leaves this machine`

### 9.2 provider 行

- Connection-type label: `API key` / `Subscription` / `Local`
- Subscription 行内: `Codex subscription — sign-in current` / `expired — run codex login` / `not detected — log in to the Codex CLI first`
- Key state chips:
  - `key set — ****1234`（api-key 已存；`delete stored key`）
  - `key from env: FIREWORKS_API_KEY`（env 路径）
  - `no key set`（api-key 未配置；`Test` 引导 401 文案）
  - `local — no key`（local-none）
  - `logged in` / `expired` / `not detected`（host-login，来自 oauth-availability；expired 附 `run codex login` 命令）
- Buttons: `Test` · `Edit` · `Delete`（builtin 无 Delete）
- Provider test in-flight: `Testing connection to Fireworks…`
- Provider test ok: `Connected — key works. Found 1,204 models.`
- Provider test 401: `Fireworks rejected the key in FIREWORKS_API_KEY — it's missing, wrong, or expired. Check it at <key url>, then paste a new one here.`
- Provider test reach (Ollama/local): `Can't reach Ollama at http://localhost:11434. Is it running? Install from ollama.com, then pull a model (ollama pull llama3.1:8b).`
- Add/edit dialog title: `Add provider` / `Edit provider — <name>`
- Connection type prompt: `How do you connect?` — cards:
  - `API key` — `Bring your own key. Paste it once — stored locally, never shown again.`
  - `Subscription` — `Reuse a Codex or Grok sign-in on this computer. No key to manage.`
  - `Local` — `No key at all — a local endpoint on this machine.`
- Name field: `name`（placeholder: `e.g. My company gateway`）
- Endpoint field: `endpoint` · `reset to <provider> default`（内建）
- Key field（api-key）: `api key` — `paste once (stored locally, never shown again)` / `or an env var name for headless/CI use`
- Key saved chip: `key stored — ****1234` + `delete stored key`
- Key saved note: `Stored under ~/.mnemoseed/secrets. Never shown again — only this masked tail. Deletable any time.`
- Delete key confirm: `Delete this key? Providers using it fail until a new key is set.`
- Save provider: `Save provider`（通过 provider 探测后武装）· `Cancel`
- Delete provider confirm（被引用）: `Delete "<name>"? N role(s) use this connection: <roles>. They fall back to their previous routes.` · buttons `Cancel` / `Delete anyway`
- Delete provider confirm（未被引用）: `Delete "<name>"? No role uses it.` · `Cancel` / `Delete`

### 9.3 角色分配面板

- Role configure header: `Configure <role>`（deep_reflection / short_increment）
- Provider prompt: `which provider?`（来自注册表的单选卡；附 `+ add provider` / `manage providers` 跳转）
- No provider selected note: `Pick a provider from the registry, then choose a model.`
- Model prompt: `model` · placeholder `type or pick a model`
- Max tokens: `max tokens`（advanced）· `blank = role default`
- Save gate（不变）: `Test the connection first — a route can only be saved after a passing probe of these exact values.`
- Bind saved banner: `route deep_reflection saved — uses Fireworks · config version <v> (audited)`
- Role card bound line: `via Fireworks · accounts/fireworks/models/kimi-k3`；unbound/default: `defaults` chip
- Shared provider note（角色绑定已共享的 provider 无需再录 key）: `This connection's key is already stored — nothing to re-enter.`

### 9.4 向导（Step 0 → 注册表，沿用 §10.4 文案 + 增补）

- Step 0 title: `How should MnemoSeed reach a model?`（不变）
- Step 0 cards（不变）: ① `Use a login on this computer` ② `Bring your own API key` ③ `Run locally on this computer`
- 注册后保存: `Connection saved — now pick the model.`
- 其余向导文案沿用 `模型路由配置-UX.md` §11.1，不变。

### 9.5 CLI onboard（增补两行）

- `1) add a new provider (API key)` / `2) use an existing provider`（注册表菜单化）
- `provider <name> saved — now bind it to a role.`
- 既有步骤文案（§11.3）不变。

---

## 10. Wireframes（ascii）

### 10.1 ⑧ 双窗格总览

```
┌─ models & routing ─────────────────────────────────────────────────────┐
│  Connections live in the provider registry. Each role picks a           │
│  connection and a model — you never enter an endpoint or key twice.     │
│  Key values never appear here — only a masked tail (****1234).          │
│  Model routing is system-scoped — set by the owner/admin and applies    │
│  to every user.                                                         │
│                                                                         │
│  ┌─ providers ─────────────────┬─ roles ────────────────────────────┐  │
│  │ providers                   │ roles                               │  │
│  │  Fireworks        API key   │  deep_reflection — the careful model│  │
│  │   api.fireworks.ai/…  ****1│  via Fireworks · kimi-k3             │  │
│  │   [Test] [Edit]             │  connected        [configure]       │  │
│  │  OpenRouter       API key   │                                     │  │
│  │   openrouter.ai/api/v1 ****9│  short_increment — the quick model  │  │
│  │   [Test] [Edit]             │  via Fireworks · deepseek-v4-flash  │  │
│  │  Anthropic        API key   │  connected        [configure]       │  │
│  │   api.anthropic.com  env ▸  │                                     │  │
│  │  Ollama           Local     │  [fully offline badge — derived,    │  │
│  │   localhost:11434  local    │   only when BOTH roles → ollama]    │  │
│  │  opencode Zen     API key   │                                     │  │
│  │  opencode.ai/zen/v1   ****7 │                                     │  │
│  │  My company gw     API key  │                                     │  │
│  │   gw.corp.internal ****2    │                                     │  │
│  │   [Test] [Edit] [Delete]    │                                     │  │
│  │  Codex            Subscrip. │                                     │  │
│  │   logged in · auto-refresh  │                                     │  │
│  │  ─────────────────────────  │                                     │  │
│  │  [+ Add provider]           │                                     │  │
│  └─────────────────────────────┴─────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────┘
```

### 10.2 添加/编辑 provider

```
┌─ Add provider ─────────────────────────────────────────────────┐
│  How do you connect?                                           │
│  ◉ API key            ○ Subscription (Codex/Grok)  ○ Local     │
│                                                                │
│  name    [ My company gateway                    ]             │
│  endpoint[ https://gw.corp.internal/v1           ]             │
│            (blank = provider default · reset for built-ins)    │
│  api key [ •••••••••••••••••1234 ]  ← paste once, never again  │
│          or env var name for headless/CI: [GW_API_KEY ]        │
│          (key saved — ****1234) [delete stored key]            │
│  ─────────────────────────────────────────────────────────────  │
│  [cancel]                      [test connection]  [save]       │
│  → ok: "Connected — key works. Found 1,204 models."            │
└────────────────────────────────────────────────────────────────┘

（选 ○ Subscription 时表单变形：无 endpoint、无 key；显示 codex/grok
  三态卡 + 粘贴 token 兜底；选 ○ Local 时：无 key 块，endpoint 默认
  localhost:11434。）
```

### 10.3 角色分配（绑定流）

```
┌─ configure deep_reflection ─ the careful model ────────────────┐
│  which provider?                                               │
│    ◉ Fireworks    ○ OpenRouter   ○ Anthropic   ○ Ollama        │
│    ○ opencode Zen ○ My company gw   ○ Codex (subscription)     │
│  This connection's key is already stored — nothing to re-enter.│
│  model [ accounts/fireworks/models/kimi-k3          ] ▾ 1,204  │
│  max tokens [ 2048 ] (advanced)                               │
│  [test connection]  ✓ connected — key works.    [save route]   │
└────────────────────────────────────────────────────────────────┘
```

### 10.4 删除 provider 确认

```
┌─ Delete "My company gateway"? ────────────────────────────────┐
│  ⚠ 2 roles use this connection:                               │
│      · deep_reflection   · short_increment                    │
│  Deleting it removes them from those roles; each falls back   │
│  to its previous route (defaults, or the route you had before │
│  this connection).                                            │
│                                        [cancel] [delete anyway]│
└───────────────────────────────────────────────────────────────┘
```

### 10.5 向导 Step 0 → 注册表

```
┌─ step 0 ─ connection ────────────────────────────────────────────┐
│  How should MnemoSeed reach a model?                              │
│  [ ① Use a login on this computer ]     → registers host-login    │
│      Reuse a Codex or Grok sign-in.       provider entry          │
│      No key to manage.                  → Step 1 三态（选①后才探测）│
│  [ ② Bring your own API key ]            → registers api-key       │
│      Fireworks · OpenRouter · Anthropic ·  provider entry          │
│      or any OpenAI-compatible endpoint.  → Step 1 服务商卡         │
│  [ ③ Run locally on this computer ]       → registers local        │
│      Ollama — free, offline, lower          provider entry         │
│      synthesis quality.                  → Step 2 直达             │
│  ───────────────────────────────────────────────────────────────  │
│  [Skip for now — capture-only ✓ dream off until you configure a   │
│   model]                                                           │
│  [continue]  (primary，选中卡后武装)                                │
└───────────────────────────────────────────────────────────────────┘
```

---

## 11. 后端需求清单（精确端点/字段/迁移）

> 全部为**新工作**；现状已有端点不在此列（`llm/routes`、`llm/test`、`llm/oauth-availability`、`llm/key`、`config/set` 均已核实存在）。新端点全在 identity gate 后；写操作 loopback-only（与 `configwrite/routes.py`、`llm/admin_routes.py` 一致）。

### 11.1 新端点

| 方法 | 路径 | 请求体（字段） | 返回要点 | 说明 |
|---|---|---|---|---|
| GET | `/api/v1/providers` | — | `{providers:[{id,name,kind,type,driver,base_url,key_state,provider}], checked_at}`；`key_state={state, masked_tail?, expires_at?}` | 注册表清单；**永不返回 key 值**；host-login 的 state 来自 `oauth-availability`（实时三态） |
| POST | `/api/v1/providers` | `{name, type, base_url?, provider?}` | `{id, name, type, ...}`；422 typed | 创建自定义条目（type=`api-key`/`local-none`）或宿主登录条目（type=`host-login`，provider 限 codex/grok）。走 configwrite（audit + versioned record + 热应用） |
| PATCH | `/api/v1/providers/{id}` | `{name?, base_url?}`（内建仅这两项+key；自定义还可 `type?`） | 同 POST 返回；409 若 type 变更使已绑定角色失效（或转 force） | 编辑。内建条目 type 不可变（§14 A6） |
| DELETE | `/api/v1/providers/{id}` | `{force?: bool}` | `{ok, freed_roles:[...]}`；409 `{in_use:[roles]}` 当被绑定且未 force | 删除 + **同一次写入清空绑定角色的 `provider` 引用**；内建返回 422 |
| POST | `/api/v1/providers/{id}/test` | `{base_url?}`（可覆写探测） | `{ok, detail:{models?, error?}}` | 直接探测 provider（用其 effective key 源）；**不武装角色保存门**（角色保存仍要求 `/llm/test` 精确签名） |
| POST | `/api/v1/providers/{id}/key` | `{key}` | `{ok, masked_tail, restart_required:false}` | 写入 `mnemoseed/providers/<id>` + 钉 `llm_providers.<id>.api_key_env = secrets:mnemoseed/providers/<id>` |
| DELETE | `/api/v1/providers/{id}/key` | — | `{ok, restart_required:false}` | 删 key + 清引用，回退 env 链 |
| GET | `/api/v1/providers/{id}/models` | — | `{models:[...]}` | provider 目录直取（`Load model list` 的 provider 级形态）；**可选本轮**（与 D2 同级，见 §14 A2） |

### 11.2 configwrite 新注册键（`CONFIG_KEY_REGISTRY` 扩展）

每个自定义/宿主登录条目 + 内建覆盖项注册为可写键，走 single writer（surgical TOML `[llm_providers.<id>.*]` + versioned record + audit + 热应用 + reconcile_boot DB-primary 参与）：

| key_path | 类型 | 校验 |
|---|---|---|
| `llm_providers.<id>.name` | string | 非空 |
| `llm_providers.<id>.type` | enum | `api-key` \| `host-login` \| `local-none` |
| `llm_providers.<id>.driver` | enum | `openai_compatible` \| `anthropic` \| `ollama` \| `oauth` |
| `llm_providers.<id>.base_url` | optional string | 空 = 驱动默认 |
| `llm_providers.<id>.api_key_env` | secret | env 名链 或 `secrets:mnemoseed/providers/<id>`（见 11.3） |
| `llm_providers.<id>.provider` | optional string | host-login 限 `codex` \| `grok` |
| `llm_providers.<id>.builtin` | bool | 内建 seed 标记（只读；定案项 §14 A6） |

角色侧：**复用现有 `dream.llm.<role>.provider`** 字段存注册表 id（已承载卡片 id/oauth 名，§2.1；A7）。`provider` 校验放开为"已知内建 id 或已注册自定义 id 或 codex/grok"。

### 11.3 secrets 引用语法扩展（必改三处）

- `secrets/refs.py`：`SECRETS_REF_RE` 扩展命名空间——`secrets:mnemoseed/dream/<role>` **保留**；新增 `secrets:mnemoseed/providers/<id>`（id 段 `[a-z0-9][a-z0-9_-]*`）。
- `config.py:_validate_api_key_ref`（行 299-321）：providers 命名段放行（校验 id 已注册或内建；内建 id 恒真）。
- `configwrite/service.py:_validate_env_name_list`（行 160-188）：同上放行；混用拒绝规则不变。
- `routing.py:resolve()`（行 88-108）无需语法改动（通用解析 `secret_name_from_ref`），但需 §11.4 的 provider→params 合并。

### 11.4 生效解析语义（绑定 provider 后的 effective 分辨率）

```
effective.driver      = role 显式 driver  ‖ provider.driver ‖ loader 默认
effective.base_url    = role 显式 base_url ‖ provider.base_url ‖ 驱动默认
effective.api_key_env = role 显式 api_key_env ‖ provider.api_key_env(含 secrets: 引用) ‖ 角色默认链
model / max_tokens    = 恒为角色字段（provider 不携带）
```

优先级 **显式 > 注册表 > 驱动默认**，与现状 `effective`（显式获胜）一致。`llm/admin.py:routes()`（行 115-164）的 `effective` 块按此扩展；`set_role`/`test_config` 的签名、探测门不变——绑定 provider 的保存 = 对 effective 路由的精确签名探测通过。角色独立解析、云+本地混搭、热应用（per-role generation）全部保留。

### 11.5 迁移（§7 的工程化）

- 后端提供一次性镜像：`GET /api/v1/providers` 在未初始化注册表时，按每个角色的 effective 路由 seed 出视图条目（known driver+default base_url → 内建 id；custom base_url → 临时 `custom-<hash>` 视图条目，`persisted:false`），前端据此渲染；保存某角色时该条目才持久化。
- 角色删除（无此操作——两角色不可删，`LLM_ROLES` 固定）与 provider 删除的清引用在 §11.1 DELETE 已定义。
- 审计 action 新增：`provider_create` / `provider_update` / `provider_delete` / `provider_key_set` / `provider_key_delete`（env 名/引用，无值）。

---

## 12. QA / Playwright 验收草稿（本轮后的后续职责）

沿用 `模型路由配置-UX.md` §15 模式（临时 `MNEMOSEED_HOME` + 备用端口 daemon，`node .bench/…-check.mjs` require `.bench/graphview-three/node_modules/playwright`，截图进 `.bench/shots/`）：

| 页面 | 断言（全部可脚本化） |
|---|---|
| ⑧ 双窗格 | `[data-providers-pane]` 与 `[data-roles-pane]` 存在；内建 provider 行可见且无 Delete；自定义行有 Delete。 |
| 添加 provider | 表单变形：api-key 有 key 块、host-login 无 key 无 endpoint 且限 codex/grok、local 无 key；`Test` 失败 401 → save 禁用且文案含 "rejected the key"；成功 → `key stored — ****<4>` chip 出现；`api_key_env` 写为 `secrets:mnemoseed/providers/<id>`。 |
| 角色绑定 | 角色卡出现注册表 provider 卡；**表单无 base_url / key 字段**（死输入断言：`[data-llm-url-*]`、`[data-key-field]` 计数 = 0）；选 provider → model catalog 填充；未探测 save disabled 且点击显示原因（409 映射）。 |
| 共享 | 角色 B 绑同一 provider 不再提示录 key；`/api/v1/providers/<id>/key` 仅被调用一次。 |
| 删除 | 被引用 provider 的 Delete 弹 ask-warning 列出两角色；`delete anyway` → `GET /api/v1/llm/routes` 中两角色 `provider` 引用已清、`effective` 回退正常、角色卡显示 defaults/needs attention；内建 Delete 不存在。 |
| 向导 | Step 0 三卡 → 各自注册对应类型 provider；`POST /api/v1/providers` 被调用；无 oauth 预渲染。 |
| 向后兼容 | 预置 legacy `[dream.llm.<role>]`（含自定义 base_url）→ 首载渲染出 registry 视图条目，无重录；保存后 TOML 规范化为 provider+binding。 |
| 窄屏 360px | 无横向滚动；provider 卡/角色卡可点。 |
| 红字 | 全链路断言 key 值不出现于任何响应体（如 `/api/v1/providers`、audit、config）。 |

---

## 13. 需要 architect / 产品拍板的风险项（A-items，未定案）

| # | 问题 | 选项 | 建议 |
|---|---|---|---|
| A1 | **角色是否仍持有 per-role driver**，还是 driver 由 provider 派生？ | (a) provider 派生（角色只剩 model+max_tokens）；(b) 角色保留 driver，provider 也带 driver，绑定校验兼容（custom 必须 openai_compatible；anthrpoc 等原生对号） | **(b)** ——保住现状"两角色独立指向任意服务商"（D10）与既有配置可解析性；避免绑定后驱动悄悄换掉造成语义惊吓。 |
| A2 | **provider catalog 跨角色缓存**：探测结果 `detail.models` 按 provider 缓存（现状 `state.llm.catalog[providerId]` 已按 provider id 缓存）vs 后端 TTL 缓存 vs 专用目录端点 | (a) 前端按 provider id 缓存（现状延续）+ (b) 本轮加 `GET /api/v1/providers/{id}/models` 作为 `Load model list` 的 provider 级形态；(c) 后端 TTL 缓存推迟 | **(a)+(b) 本轮**；(c) 后续。缓存只属于 provider，绝不跨 provider 串模型。 |
| A3 | **向后兼容优先级**：legacy `dream.llm.<role>.base_url/api_key_env` vs provider 条目 | (a) 显式角色值 > 注册表 > 驱动默认；(b) 注册表恒胜 | **(a)**——与现状 `effective` 显式获胜一致，迁移期角色旧值仍有效。 |
| A4 | **注册表持久化介质**：configwrite 键（surgical TOML `[llm_providers.*]` + versioned + audit + DB-primary reconcile）vs meta DB 独立表 vs 独立 providers.toml | (a) configwrite 键 | **(a)**——单写路径、循环回归少；注意 reconcile_boot 会把 provider 键纳入 DB-primary（一致，可接受）；boot-scope 语义不需要。 |
| A5 | **provider 级 secrets 引用命名空间扩展**：`secrets:mnemoseed/providers/<id>`（改 refs.py 正则 + 两处校验） | (a) 扩展语法，角色级引用保留；(b) 不改语法，用 role 级引用的特殊约定 | **(a)**——语法是显式契约，约定式 hack 会让审计/校验混浊。 |
| A6 | **内建 provider 可否编辑/删除**：只读模板（仅 name/base_url 覆盖 + key 保管持久化）vs 全字段可编辑 | (a) 只读模板 + 覆盖项持久化；type 不可变 | **(a)**——`fireworks` 永远 api-key；避免用户把内建改成 host-login 造成校验与文档双漂移。 |
| A7 | **角色绑定用现有 `provider` 字段还是新字段 `provider_ref`** | (a) 复用 `provider`（已承载卡 id/oauth 名）；(b) 新 `provider_ref` | **(a)**——少一个 schema 变更、`llmActiveProviderId` 反查天然可用；代价是"other"卡丢弃逻辑（`admin.py:493`）改为"写真实注册表 id"。 |
| A8 | **宿主登录（opencode Zen / Codex / Grok）复用深度**：粘贴一次（现状）vs daemon 直接读 `~/.local/share/opencode/auth.json` 的 `opencode-go`（新增 host-login 提供者） | (a) 本轮粘贴一次；(b) 后端加 opencode host-login 提供者 | **(a) 本轮**；(b) 留给工程师（现状 leftover，`模型路由配置-UX.md` §12.1）。 |

**A1/A3/A7 建议与后端需求 §11.4 绑定**，若拍板变化则 §11.4 的优先级/字段随之改。

---

## 14. 文档同步清单（定案后随 change set 一次性落实）

- `模型路由配置-UX.md`：⑧ 编辑器章节（§10.1）改引本规格双窗格；`LLM_PROVIDERS`（§2.2）改指注册表 seed；`§14.2` 交接地图更新。
- `07-管理控制台.md` §8：路由表由两角色行扩展为"注册表 + 绑定"两段描述。
- `design/02 §6`：默认值不变（两角色默认仍 Fireworks）。
- PRD-07 对应 FR：`G-AC2` 文案不动；新增 provider 注册表能力的 FR 条目待 PRD 侧补。
- 命令行 `llm set --help` / `onboard`：provider 菜单化（§9.5）。
