# PRD-05 · 云端同步、TEE 梦境与计费网关（MnemoSeed Cloud）

> 对应设计文档：[04-隔离解耦与隐私](../design/04-隔离解耦与隐私.md)、[08-多端同步与冲突合并](../design/08-多端同步与冲突合并.md)（同步协议与冲突合并语义以 08 为准）
> 里程碑：M4（商业化）· 预估 30 天

## 1. 目标

上线 $9/月 MnemoSeed Cloud：3 Profile 物理隔离、多端 E2EE 同步、Nitro Enclaves 内做梦、动态模型路由套利网关。云端全链路零明文持久化。

## 2. 范围

- **In**：E2EE 同步协议、多 Profile 隔离、Enclave 梦境执行环境 + attestation、动态路由网关（Sonnet / GPT-5.6 Terra）、算力币计费
- **Out**：企业私有化集群（License 渠道单独谈）

## 3. 功能需求

| ID | 需求 | 优先级 |
|---|---|---|
| FR-5.1 | BYOK：客户端私钥派生加密，密钥永不出设备；云端只存密文 blob | P0 |
| FR-5.2 | 多端同步：密文 blob + provenance 时间序重放，断网队列恢复 | P0 |
| FR-5.3 | Profile 隔离：单账号 ≤3 个独立 Profile，跨 Profile 零数据泄漏 | P0 |
| FR-5.4 | Enclave 梦境：记忆仅在 Nitro Enclave 内短暂解密，反思完成即焚毁明文 | P0 |
| FR-5.5 | Attestation 验证接口：客户端可密码学校验 Enclave 内跑的是官方未篡改镜像 | P0 |
| FR-5.6 | 动态路由：长背景深反思 → Kimi K3（Fireworks，cache read $0.30/M）；短增量（动态预算 ≤32k，PRD-02 FR-2.5） → DeepSeek V4 Flash 0731（Fireworks，$0.14/M input） | P0 |
| FR-5.7 | 计费：Profile 数量 + 脑容量算力币混合计费；$5 = 1M 算力点；免费版每月 500k 增量做梦额度（标准版内） | P0 |
| FR-5.8 | 云端多用户账号体系：邮箱注册 + Google sign-up（OAuth 绑定官方域名）；团队邀请与席位管理 | P0 |
| FR-5.9 | Commercial License 渠道：自部署多用户激活（Ed25519 签名 license 离线验签，entitlements: multi_user/seats/有效期）；到期宽限 30 天，超期多用户登录停用但 owner 与数据完好（永不动数据）；自部署可自配 Google OAuth client | P0 |
| FR-5.10 | Admin Plane 超管界面：服务健康/增长销售（注册/漏斗/MRR/license 激活）/用户运营（配额/封禁）/计费成本（模型路由分解/毛利率/TEE 利用率）；**红线：仅运营元数据，记忆明文物理不可见（BYOK 架构保证，非纪律）**；超管独立强认证（TOTP/硬件密钥），全部操作进不可变审计日志 | P0 |

## 4. 非功能需求

| ID | 需求 |
|---|---|
| NFR-5.1 | 仅使用官方企业渠道（AWS Bedrock / Vertex AI）+ ZDR 端点；禁止 OpenRouter 类中介 |
| NFR-5.2 | 单台 TEE 物理成本 ≤ $80/月，支撑 ≥ 200 付费用户（毛利率 ≥ 60%） |
| NFR-5.3 | 合规：GDPR / CCPA / PDPA 数据处理协议就绪 |
| NFR-5.4 | 同步冲突合并语义以 design/08 为准：append-only 数据 CRDT 收敛（chunks=G-Set、积分池=PN-Counter、图谱=content-hash），真矛盾不选边、走 Reconcile flag_conflict 显性化；删除走 Tombstone OR-Set |

## 5. 验收标准

- AC-1：拔掉云端数据库直接审计——零明文；
- AC-2：两台设备交替对话，记忆收敛一致且历史链完整；
- AC-3：第三方安全研究员用 attestation 接口验证 Enclave 镜像哈希；
- AC-4：计费沙盘：100 重度用户月消耗模型 API 成本 ≤ 算力币收入的 40%。

## 6. 任务拆分

1. `cloud/sync` —— E2EE 同步协议 + 断网队列（6d）
2. `cloud/enclave` —— Nitro Enclave 镜像 + attestation（8d）
3. `cloud/router` —— 动态路由网关 + Prompt Cache 管理（5d）
4. `cloud/billing` —— 算力币账本 + Stripe 对接（5d）
5. 安全审计 + 合规文档（6d）

## 7. 依赖

- M1–M3 全部完成（本地闭环已验证、社区信任资产就位）
