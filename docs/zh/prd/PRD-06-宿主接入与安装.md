# PRD-06 · 宿主接入与安装体验（daemon + installer + Tier 1 宿主适配）

> 对应设计文档：[06-接入与安装体验](../design/06-接入与安装体验.md)
> 里程碑：M1 · 预估 9.5 天（2026-08-08 宿主实测调研后修订）· v1.1 · 2026-08-13

## 1. 目标

一条命令、三分钟、零账号，把 MnemoSeed 装进用户已有的所有 AI 宿主，并在下一个 session 自动产生第一次"它记得我"的体验。

## 2. 范围

- **In**：mnemoseed-daemon（embedded 模式）、installer（探测/注册/doctor）、Tier 1 宿主适配（Claude Code plugin / Cursor hooks / Codex CLI hooks / Gemini CLI extension）、MCP instructions 降级模式（Tier 2 地板）、uninstall
- **Out**：云同步登录（PRD-05）、docker compose 全家桶（M0 已有骨架）、**Tier 2 桌面 Chat 深化**（.mcpb 打包、MCP Apps 记忆 UI、ChatGPT hosted 端点/tunnel——只做 MCP server 顺手附带，不进 M1 验收；OpenCode/Windsurf 适配 P2 顺延）

> 宿主分档与实测能力矩阵见 design/06 §2（2026-08-08 官方文档实测版）。

## 3. 功能需求

| ID | 需求 | 优先级 |
|---|---|---|
| FR-6.1 | installer：宿主探测（~/.claude.json、~/.cursor/、codex config）→ 用户确认接入清单 → 写注册前备份 + diff 预览 + 逐项确认 | P0 |
| FR-6.1b | `mnemoseed login [--baseurl]`：本地免密确认 / 云端账号认证；profile 创建与 token 签发（identity 模型见 design/06 §2.6） | P0 |
| FR-6.1c | `mnemoseed link/unlink`：逐 agent 选 profile，把 `profile_id + token` 以 env 形式写入各 agent 原生配置（MCP 条目 / OpenClaw/Hermes 各自 config / Claude Code user+project scope / Codex `shell_environment_policy.set`）；宿主界面永远只有一条 mnemoseed | P0 |
| FR-6.1d | `mnemoseed whoami`：当前环境身份自证（profile/daemon/token 有效性）；`mnemoseed status` 回读全部宿主配置生成绑定总表 | P0 |
| FR-6.1e | 首次注册流程：daemon 首启无 owner → setup 态（记忆 API 503 + 指引，仅 `/console/setup` 可用）；setup 创建 owner（argon2 密码哈希），仅允许一次，此后端点永久关闭；`mnemoseed auth reset` 本机重置密码 | P0 |
| FR-6.1f | 开源版单用户+单 profile 硬限：用户管理仅 owner，"添加用户"锁定并注明激活路径（官方云端 / commercial license）；profile 创建同样硬限 1 个、第二个入口锁定并注明激活路径；license 激活入口（Ed25519 离线验签，entitlements 含 multi_user/seats/profiles/有效期，到期宽限 30 天且永不动数据） | P0 |
| FR-6.2 | daemon embedded 模式：单进程内嵌 LanceDB + SQLite-Graph + bge-m3 ONNX（~543MiB int8 量化模型下载含断点续传），零 Docker 依赖（默认栈选型见 design/03 §1） | P0 |
| FR-6.3 | Claude Code plugin（marketplace 分发，单包含 hooks + MCP + commands + skills）：SessionStart 暖场注入（additionalContext ≤800 tokens，远低于 10,000 字符上限）/ **UserPromptSubmit 逐轮注入**（daemon 2s 内返回高相关记忆，超时/空结果 fail-open 不注入）/ UserPromptSubmit+PostToolUse 自动捕获 / PreCompact flush / SessionEnd 结算（Stop hook 遵守连续 8 次 block 上限与 stop_hook_active 检查）；slash commands：/memory /dream /forget /recall | P0 |
| FR-6.3b | Cursor 适配：`.cursor/hooks.json`（项目级）——afterAgentResponse 全文捕获 + postToolUse 捕获 + sessionStart 暖场注入；`.cursor/rules/*.mdc`（alwaysApply）常驻读取指引；**逐轮注入不可用**（beforeSubmitPrompt 只能 block），读取靠暖场 + rules + MCP recall；验证 Claude Code hooks 兼容层可复用程度 | P0 |
| FR-6.3c | Codex CLI 适配：`~/.codex/hooks.json`——SessionStart 暖场 + UserPromptSubmit 逐轮注入（≤2,500 token）与捕获 + SessionEnd transcript 结算；AGENTS.md 常驻指引；**installer 必须引导用户完成 `/hooks` trust 审查**（按 hash，否则 hooks 静默不执行） | P1 |
| FR-6.3d | Gemini CLI 适配：extension 单包（MCP + GEMINI.md + hooks + commands）——SessionStart 暖场 + BeforeAgent 逐轮注入 + AfterTool 捕获 | P1 |
| FR-6.4 | hook 直连 daemon localhost HTTP，不经 MCP，2s 超时 fail-open，零 token 消耗 | P0 |
| FR-6.5 | MCP initialize `instructions` 降级模式行为指引（Tier 2 宿主），**文案 ≤512 字符自包含**（Codex 官方建议上限，Claude Code 2KB 截断取更严者），配合 remember 幂等去重 | P1 |
| FR-6.6 | `mnemoseed doctor`：daemon 存活 / 端口 / embedding 加载 / round-trip 存取实测 / 宿主注册生效，单项失败给单行修复命令 | P0 |
| FR-6.7 | `mnemoseed uninstall`：逐宿主注销（备份恢复或精确摘除）、停 daemon、数据默认保留并明示路径、--purge 才删 | P1 |
| FR-6.8 | 配置单一事实源 `~/.mnemoseed/config.toml`；宿主侧仅瘦注册 | P0 |
| FR-6.9 | 首次设置 LLM 向导（`mnemoseed onboard` 的 post-setup 步骤，FR-6.10）：引导梦境模型配置，推荐顺序 ① OAuth 复用订阅（Codex / Grok 本地登录态，条款允许；Anthropic 订阅不复用；中国用户可选 MiniMax/Kimi 等 CLI 服务商，选择时明示数据出境提示）② 自带 API key（任意 OpenAI 兼容端点，如 Fireworks）③ 高级离线轨（Ollama，≤14B 量化模型，附提炼质量警告）；连通性实测通过才写入 config.toml；落地 PRD-02 FR-2.14 的角色路由 | P0 |
| FR-6.10 | `mnemoseed onboard`：在既有原语之上的引导式逐步聚合——① owner 账号设置 → ② 存储形态选择 → ③ 梦境 LLM 向导（FR-6.9）→ ④ 宿主链接 → ⑤ 开机自启 → ⑥ doctor 全绿。规则：① 与 console 设置向导共享**同一个**后端 onboard 服务——无并行逻辑（`/api/v1/setup` 端点保持 exact-once）；② LLM 向导是 post-setup 步骤（保持"连通性实测后才持久化"行为）；③ 每步可跳过 + 可续跑——跳过 LLM 步骤得到可启动的仅捕获 daemon，向导内明示；④ 宿主链接步骤原样复用安装的备份 + diff 预览 + 逐项确认纪律（FR-6.1）；⑤ TTFM < 3 分钟仍为 happy-path 目标，每步限时；⑥ 配置操作仅限 loopback——非 loopback baseurl 直接报清晰错误 | P0 |

## 4. 非功能需求

| ID | 需求 |
|---|---|
| NFR-6.1 | Time-to-First-Memory < 3 分钟（全新机器、正常网速、embedded 模式） |
| NFR-6.2 | hook 注入/捕获 p95 < 50ms，daemon 不可达时宿主体验零影响（fail-open） |
| NFR-6.3 | 对用户既有配置的所有修改可回滚（备份文件带时间戳保留 30 天） |

## 5. 验收标准

- AC-1：全新 Windows/Mac 机器单条命令完成安装，doctor 全绿，总耗时 < 3 分钟；
- AC-2：装完不开任何新窗口，下次 Claude Code session 开场自动出现近期记忆摘要注入；
- AC-3：session 中说一句"以后我用 pnpm"，不调用任何工具，次日新 session 暖场摘要中出现该偏好；
- AC-4：uninstall 后各宿主配置恢复安装前原样，diff 为空；
- AC-5：daemon 进程杀掉后 Claude Code 正常启动、正常对话，仅无记忆注入；
- **AC-6（逐轮确定性捕获）**：Claude Code session 中每轮用户输入经 UserPromptSubmit hook 写入 daemon——零 token 消耗、零模型参与（host transcript 中无记忆相关工具调用记录）；Cursor 中每轮 assistant 回复经 afterAgentResponse 全文捕获；
- **AC-7（逐轮注入）**：Claude Code / Codex CLI 中，用户在 session 中段提出与历史记忆相关的问题（不 @ 任何内容、不提示用记忆），该轮 prompt 旁自动出现 daemon 注入的相关记忆（additionalContext），p95 注入延迟 < 2s 且超时不阻塞对话；
- **AC-8（Codex trust 引导）**：全新 Codex CLI 环境安装后，installer 输出 `/hooks` trust 引导，用户完成 trust 后 hooks 全部生效；
- **AC-9（Tier 2 地板）**：仅配 MCP 的环境（模拟桌面 Chat 场景）中，daemon instructions 字段 ≤512 字符下发，模型在系统提示之外能自主完成一次 recall→回答→remember 闭环（概率性，不做 100% 要求）。

> Tier 2 桌面 Chat（Claude Desktop Chat / ChatGPT 产品面）不进 M1 验收，见 design/06 §2.2–2.3。

## 6. 任务拆分

1. `daemon/embedded` —— 单进程打包（uv 分发为主路径，见 design/03 §1）（2d）
2. `installer/` —— 探测/注册/备份/doctor/uninstall + Codex `/hooks` trust 引导（3d）
3. `plugins/claude-code/` —— hooks（含 UserPromptSubmit 逐轮注入）+ slash commands + marketplace 清单（2d）
4. `adapters/cursor/` —— hooks.json + rules 模板（1d）
5. `adapters/codex/` + `adapters/gemini/` —— hooks + AGENTS.md/GEMINI.md 指引片段（1d）
6. MCP instructions（≤512 字符）+ 降级模式 e2e（0.5d）

> 预估总计约 9.5 天（原 8 天 + Cursor/Codex/Gemini 适配 1.5 天）。

## 7. 依赖

- M0（schema 基座）、PRD-01（Capture 漏斗）、PRD-03（digest/recall API）
