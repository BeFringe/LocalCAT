# 设计约束清单 — TM Store Candidate 模块提取

## 设计来源

- 项目 owner 批准“TM Store 治理 → TM Store 解耦”，并批准完整 Gate C/D 成本。
- `tm-store-module-extraction` Requirements/Design/Tasks。
- ADR-007/008/009/010/013/016 与新增 ADR-017。
- `tm-storage-retrieval-index` 已完成的 R1–R3 行为保持型提取与 current-source evidence。

## 红线

| 约束 | 验证锚点 |
|---|---|
| 不改变 schema/version/persistent objects | schema snapshot、DDL、migration fixture exact equality |
| 不改变 scorer/budget/proof-query/ordering/threshold/top-k | retrieval/proof report exact equality；Gate C/D硬门不变 |
| coordinator/generation/transaction/capability 不迁权 | projection禁止connect/commit/rollback/coordinator/capability import |
| algorithm不依赖concrete store | `tm_candidate_index` import closed-world |
| 叶合同与SQL各只有一个authority | object identity + SQL owner AST guard |
| compatibility wrapper不成为第二实现 | wrapper只late-bound委托；无复制SQL/validator |
| current-source变化必须真实重验 | Gate C roots + Gate D final fingerprint + owner evidence |
| 不阻塞或污染Multi-Document主线 | Parser/Qt/Multi-Document/Chunk/Sync/ResourcePackage零功能diff |

## 灰线

- 历史 `SQLiteCandidate*` 名称可保留，以保护 exact class object 与 import surface；本规格不以改名证明中立。
- 无法证明无消费者的 late-bound wrapper 可保留；但wrapper不得持有SQL或独立validator。
- schema/bootstrap 与 completed-authority rehydration可作为未来维护候选，但不得进入当前Wave。
- `tm_contracts.py` 拆分另立 Spec，不因本次叶合同提取顺带实施。

## 降级决策记录

> 未经项目 owner 明确批准，不允许降级 Gate C/D、移除fallback、放宽proof、改变transaction或跳过current-source evidence。

| 日期 | 降级 | 原因 | owner批准 |
|---|---|---|---|
| （空） | | | |

## 归档门

- [ ] Wave 0–4 与全部 cluster review 通过
- [ ] final source roots 上 Gate C 通过
- [ ] 同一 final fingerprint 上 100k FTS5/fallback Gate D 通过
- [ ] fault/acceptance/release strict evidence GO
- [ ] Steering 与 tasks completion 同实际 tree 一致
- [ ] Multi-Document 等相邻主线无越界

> 当前状态：执行中；Feature GO 后追加归档日期与最终约束状态。
