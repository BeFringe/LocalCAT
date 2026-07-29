# Feature 5 抢救性复核

## 当前判定

法证恢复提交 `87c6513` 忠实保存了原 Requirements、Design、Tasks 和审批状态，但恢复后的交叉复核发现三个既存阻断点。修订后的 86 条验收标准已经通过 Requirements Review Gate 和人工批准；Design 已完成系统修订、三方技术审计和人工批准。Tasks 随后按批准后的 Design 重新生成，经过两轮本地 review gate 与一次独立修复后复审，最终判定为 `PASS`。当前 Requirements、Design、Tasks 均已批准，`ready_for_implementation=true`；实现必须以本基线为准逐任务推进。

## 阻断点 R1：Matcher capability 单一权威断链

- Feature 5 已拥有 `SearchOptions`、`TextMatcher`、Unicode/CJK 语义和原文 offsets，却没有正式定义 matcher capability/readiness。
- Qt increment 因此在本地声明了 `MatcherReadiness`、`MatcherCapability` 和三态 gate，形成第二权威。
- 本轮重写 Requirement 9：Core 同时拥有调用用途、三态能力、基础与完整验证 cohort、fail-closed 执行门、语义版本、同次判定摘要和不可用原因。
- `BASIC_VALIDATED` 现在同时证明 legacy compatibility 与 `match_case=false, whole_word=false` 的 Unicode case-fold 连续搜索；`TEXT_V1_VALIDATED` 在有效 BASIC 之上证明完整 Match Case / Whole Word 组合。
- 本轮 Feature 5 Design 已把 capability 类型、验证证据和状态转换定义在 Qt 无关契约中；Tasks 2.4–2.6 已分别冻结 validation manifest/evaluator、gated matcher 和 Gate A/matcher 独立发布证据。
- Qt increment 后续修订只引用 Core capability，并保留自身 `ProjectToolCapability`、控件禁用和解释职责，不得自行推断状态。

## 阻断点 T1：sealed stage 顺序矛盾

- 原 Tasks 4.3/4.5 在候选索引闭合前就把 working copy 描述为 sealed。
- 原 Tasks 7.1 随后又要求向 sealed stage 写入候选索引，违反 sealed artifact 不可再写的含义。
- 修订后的 Requirement 2 已补入完整版本发布、切换原子可见、已有版本激活失败回退和首次迁移失败继续使用原 JSONL 的可观察保证；sealed/index/fsync 等实现顺序仍留给 Design。
- 原 Design 的正确顺序保持为：`Stage → Build indexes → integrity/count/exact parity → close/fsync/seal → coordinator activate`。
- 重建后的 Tasks 5.2–5.7 已固定顺序：mutable stage 完成全部记录/索引/receipt/manifest，随后校验、关闭、fsync、seal，通过 Gate B 后才持久化 PREPARED journal 并由唯一协调器激活。

## 阻断点 T2：sidecar 身份与 source divergence 未进入 Tasks

- 修订后的 Design 规定 deterministic same-directory sidecar、JSONL 兼容入口、receipt/manifest ancestry 和 `SOURCE_DIVERGED` fail-stop。
- 修订后的 Requirements 已冻结资源身份与 Active/Lookup/Update 选择不变、双向禁止隐式覆盖、last-known-good Lookup、canonical Update 继续写入、写入不清除 divergence，以及只允许显式 import/rebuild 成功消歧。
- 原 Tasks 没有明确验收 `name.jsonl → name.jsonl.sqlite3` 映射、`ResourceConfig.path` 仍指向 JSONL、激活后 JSONL 改变时保留 last-known-good sidecar，以及只允许显式 canonical import/rebuild 消除 divergence。
- 重建后的 Tasks 已把这些行为分别落到 schema/path contract、来源状态机、preflight、Legacy facade、普通 merge、显式消歧、两类导出和最终故障矩阵。

## Design 修订结果

- Core 现在唯一拥有 TextMatcher 三态、用途 profile、验证 evidence、fail-closed outcome 和同一不可变 capability snapshot；Qt/Glossary 不再有第二权威。
- working stage 完成全部 records/candidate indexes/receipt/manifest 后才可 seal；`SealedArtifactRef`、single-use token、durable activation journal 和 coordinator 唯一入口共同阻止半成品发布。
- physical canonical activation、CONTEXT correctness、FUZZY benchmark 与 matcher capability 分别发布；FUZZY 或 matcher 失败不得把已激活 SQLite 回退到 JSONL。
- SQLite 激活后是唯一运行时读写权威；JSONL 与相邻 manifest 是绑定到 canonical 历史 revision 的只读快照。正常 local write 只形成 `VERIFIED_HISTORY`，只有 receipt/identity/digest/ancestry 不一致才产生 `SOURCE_DIVERGED`。
- activation 与 export 都有 DB/JSONL/manifest 多文件崩溃恢复矩阵；回滚必须同时恢复 prior DB 与 prior manifest/binding。
- 候选召回 metadata 与最终 Query metadata 已分层，阶段计数、overlap、dedupe、truncate、score 和 global-limit returned count 可完整对账。
- 需求追踪覆盖 9 项需求的全部 86 条验收标准。

## Tasks 重建结果

- 9 项需求的 86 条验收标准全部映射，无缺失、无额外或无效编号。
- 51 个子任务均有可观察完成条件；保留的 `(P)` 任务均有不重叠边界与显式依赖。
- 预折叠文本不被二次折叠，FTS5 tokenizer 不承担产品 Match Case 语义。
- 发布顺序固定为 Gate A/matcher → Gate B → physical canonical → Legacy parity → Gate C → Gate D。
- durable activation journal 的 `PREPARED` 必须先于任何 DB/manifest replace。
- 任意路径 export 与配置 JSONL snapshot refresh 分开实施；前者不改变活动 binding，后者只允许未 diverged 资源并遵守 issued-receipt 恢复矩阵。
- capability evidence、sealed artifact、snapshot recovery、CONTEXT/FUZZY 分门和召回/最终 metadata 分层均已进入可观察完成条件。

## 阶段门

1. 已审阅并批准修订后的 Requirements 1、2、7、9。
2. 已人工批准并通过三方技术审计的 Feature 5 Design。
3. 已重新生成并预批准 Feature 5 Tasks；独立复审最终为 `PASS`，86/86 AC 覆盖。
4. **当前：** Feature 5 进入实现阶段，按“代码 + 对应任务勾选”小步提交。
5. Qt increment 已纠正为只消费 Core capability；在 Feature 5 稳定契约提交交付前保持 matcher 入口 disabled。
