# PRD-06 · 宿主接入与安装体验（daemon + installer + Claude Code plugin）

> 对应设计文档：[06-接入与安装体验](../design/06-接入与安装体验.md)
> 里程碑：M1 · 预估 8 天

## 1. 目标

一条命令、三分钟、零账号，把 MnemoSeed 装进用户已有的所有 AI 宿主，并在下一个 session 自动产生第一次"它记得我"的体验。

## 2. 范围

- **In**：mnemoseed-daemon（embedded 模式）、installer（探测/注册/doctor）、Claude Code plugin（hooks + slash commands）、MCP instructions 降级模式、uninstall
- **Out**：云同步登录（PRD-05）、docker compose 全家桶（M0 已有骨架）

## 3. 功能需求

| ID | 需求 | 优先级 |
|---|---|---|
| FR-6.1 | installer：宿主探测（~/.claude.json、~/.cursor/、codex config）→ 用户确认接入清单 → 写注册前备份 + diff 预览 + 逐项确认 | P0 |
| FR-6.2 | daemon embedded 模式：单进程内嵌 Chroma + SQLite-Graph + embedding-gemma（量化模型下载含断点续传），零 Docker 依赖 | P0 |
| FR-6.3 | Claude Code plugin（marketplace 分发）：SessionStart 暖场注入（additionalContext ≤800 tokens）/ UserPromptSubmit+PostToolUse 自动捕获 / PreCompact flush / SessionEnd 结算；slash commands：/memory /dream /forget /recall | P0 |
| FR-6.4 | hook 直连 daemon localhost HTTP，不经 MCP，2s 超时 fail-open，零 token 消耗 | P0 |
| FR-6.5 | MCP initialize `instructions` 降级模式行为指引（非 hook 宿主），配合 remember 幂等去重 | P1 |
| FR-6.6 | `mnemoseed doctor`：daemon 存活 / 端口 / embedding 加载 / round-trip 存取实测 / 宿主注册生效，单项失败给单行修复命令 | P0 |
| FR-6.7 | `mnemoseed uninstall`：逐宿主注销（备份恢复或精确摘除）、停 daemon、数据默认保留并明示路径、--purge 才删 | P1 |
| FR-6.8 | 配置单一事实源 `~/.mnemoseed/config.toml`；宿主侧仅瘦注册 | P0 |

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
- AC-5：daemon 进程杀掉后 Claude Code 正常启动、正常对话，仅无记忆注入。

## 6. 任务拆分

1. `daemon/embedded` —— 单进程打包（PyInstaller/Bun compile 评估）（2d）
2. `installer/` —— 探测/注册/备份/doctor/uninstall（3d）
3. `plugins/claude-code/` —— hooks 四件套 + slash commands + marketplace 清单（2d）
4. MCP instructions + 降级模式 e2e（1d）

## 7. 依赖

- M0（schema 基座）、PRD-01（Capture 漏斗）、PRD-03（digest/recall API）
