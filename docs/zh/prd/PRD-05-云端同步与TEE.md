# PRD-05 · 云端托管 daemon 与计费（MnemoSeed Cloud）

> 对应设计文档：[04-隔离解耦与隐私](../design/04-隔离解耦与隐私.md)、[08-多端同步与冲突合并](../design/08-多端同步与冲突合并.md)（同步协议与冲突合并语义以 08 为准）
> 里程碑：M4（商业化）· 预估 30 天

## 1. 目标

上线 MnemoSeed Cloud：daemon 云端托管（SaaS）——宿主侧只装瘦工具（MCP/hooks），daemon 由我们部署运维；功能与自部署完全一致，仅账号/profile 限额不同。E2EE 传输 + 加密存储与部署位置无关（design/04 §3）；梦境 LLM 出口只走 ZDR 端点。TEE 规格作为增值部署选项（我们可售、用户也可自建），不是架构前提。

## 2. 范围

- **In**：云端托管 daemon（多租户隔离）、多端接入与同步、动态路由网关、用量计费、TEE 规格部署选项
- **Out**：企业私有化集群（License 渠道单独谈）

## 3. 功能需求

| ID | 需求 | 优先级 |
|---|---|---|
| FR-5.1 | 加密存储与传输：daemon 持久层落盘即密文；宿主工具 ↔ daemon 全程 E2EE + token 鉴权；密钥派生与管理在 daemon 侧，自部署时用户自持 | P0 |
| FR-5.2 | 多端同步：密文 blob + provenance 时间序重放，断网队列恢复 | P0 |
| FR-5.3 | Profile 隔离：单账号 ≤3 个独立 Profile，跨 Profile 零数据泄漏 | P0 |
| FR-5.4 | TEE 部署选项：daemon 可运行于 Nitro Enclave 规格（我方 SaaS 增值档 / 用户自建均可）；非 TEE 部署下隐私承诺 = E2EE 传输 + 加密存储 + ZDR 出口 | P1 |
| FR-5.5 | Attestation 验证接口（仅 TEE 档）：客户端可密码学校验 Enclave 内跑的是官方未篡改镜像 | P1 |
| FR-5.6 | 动态路由：长背景深反思 → Kimi K3（Fireworks，cache read $0.30/M）；短增量（动态预算 ≤32k，PRD-02 FR-2.5） → DeepSeek V4 Flash 0731（Fireworks，$0.14/M input） | P0 |
| FR-5.7 | 计费：Profile 数量 + 用量的混合计费模型；具体定价为商业决策，不在本文档（见内部市场文档） | P0 |
| FR-5.8 | 云端多用户账号体系：邮箱注册 + Google sign-up（OAuth 绑定官方域名）；团队邀请与席位管理 | P0 |
| FR-5.9 | Commercial License 渠道：自部署多用户激活（Ed25519 签名 license 离线验签，entitlements: multi_user/seats/有效期）；到期宽限 30 天，超期多用户登录停用但 owner 与数据完好（永不动数据）；自部署可自配 Google OAuth client | P0 |
| FR-5.10 | Admin Plane 超管界面：服务健康/增长运营（注册/漏斗/license 激活）/用户运营（配额/封禁）/成本观测（模型路由分解/资源利用率）；**红线：仅运营元数据；TEE 档下记忆明文物理不可见（硬件保证），非 TEE 档靠加密存储 + 最小化运行时暴露面**；超管独立强认证（TOTP/硬件密钥），全部操作进不可变审计日志 | P0 |

## 4. 非功能需求

| ID | 需求 |
|---|---|
| NFR-5.1 | 仅使用官方企业渠道（AWS Bedrock / Vertex AI）+ ZDR 端点；禁止 OpenRouter 类中介 |
| NFR-5.2 | 单节点成本与用户容量配比须支持正毛利（具体数值目标为商业决策，见内部市场文档） |
| NFR-5.3 | 合规：GDPR / CCPA / PDPA 数据处理协议就绪 |
| NFR-5.4 | 同步冲突合并语义以 design/08 为准：append-only 数据 CRDT 收敛（chunks=G-Set、积分池=PN-Counter、图谱=content-hash），真矛盾不选边、走 Reconcile flag_conflict 显性化；删除走 Tombstone OR-Set |

## 5. 验收标准

- AC-1：拔掉云端数据库直接审计——零明文（落盘全密文）；
- AC-2：两台设备交替对话，记忆收敛一致且历史链完整；
- AC-3（TEE 档）：第三方安全研究员用 attestation 接口验证 Enclave 镜像哈希；
- AC-4：计费沙盘：重度用户群的模型 API 成本占用显著低于收入（阈值随定价定案时落地）。

## 6. 任务拆分

1. `cloud/sync` —— E2EE 同步协议 + 断网队列（6d）
2. `cloud/enclave` —— Nitro Enclave 镜像 + attestation（TEE 档，P1）（8d）
3. `cloud/router` —— 动态路由网关 + Prompt Cache 管理（5d）
4. `cloud/billing` —— 用量账本 + 支付对接（5d）
5. 安全审计 + 合规文档（6d）

## 7. 依赖

- M1–M3 全部完成（本地闭环已验证、社区信任资产就位）
