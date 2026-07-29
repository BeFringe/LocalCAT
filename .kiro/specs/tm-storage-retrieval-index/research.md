# 研究与设计决策

## 摘要

- **功能**：`tm-storage-retrieval-index`
- **Discovery 范围**：复杂 brownfield integration（完整 discovery）
- **关键发现**：
  - 当前 exact 是 raw source 的区分大小写/空白字典查询；旧 JSONL 重复 source 由最后一条有效记录胜出，Excel 只认识三态。这些是 migration parity 的基线，不是待“清理”的偶然行为。
  - 每个 TM 资源独立数据库最符合现有 file-per-resource、Active/Lookup/Update 和 last-known-good 隔离；单一全局数据库会扩大损坏与迁移故障域。
  - 当前 Python 3.14.6 链接 SQLite 3.51.2，官方确认该版本处于并发 WAL-reset 损坏竞态影响范围；首版必须使用 rollback journal，WAL 只能经版本和并发测试 capability gate。
  - FTS5 trigram 适合候选召回，但短于 3 个 Unicode 字符不能通过 MATCH 命中，external-content index 还要求应用维护一致性；因此需要受控 contentful fast path 与自有 n-gram fallback。
  - Python `str.casefold()` 可能扩展字符，匹配结果必须通过 projection map 回到原字符串；stdlib `re \b` 不能充当完整 Unicode word-break 契约。
  - physical canonical activation、fuzzy benchmark 和 matcher validation 是三个独立发布门；后两者失败不能把已激活 SQLite 回退成 JSONL。
  - 迁移后 SQLite 是唯一运行时读写权威；JSONL 是绑定到 canonical 历史基点的只读快照。合法 local write 使快照变旧但不使其异源，`SOURCE_DIVERGED` 不能用“当前 DB 内容等于 JSONL”判断。

## 研究记录

### 当前 exact、JSONL 与 Excel 基线

- **查阅来源**：`tm_engine.py`、`logic_controller.py`、`excel_adapter_openpyxl.py`、`editor_controller.py` 与相关 tests。
- **发现**：
  - `TMEngine` 把 JSONL 全量载入 `dict[source, TMMatch]`，后记录覆盖前记录。
  - exact key 不做 case-fold、trim 或 normalization。
  - loader 只恢复 source/target，当前没有 canonical context/provenance。
  - `LogicController` 保持 `TM_HIT / TERMS_FOUND / NO_MATCH`，Excel formatter 对第四种状态报错。
  - Qt suggestion seam 已有 type/similarity/provenance 展示，但 `apply_tm_suggestion()` 以 suggestion.source 等于当前 source 作为 stale guard。
- **设计影响**：
  - compatibility facade 继续返回一个 raw exact winner。
  - fuzzy result 必须同时区分 `query_source` 与 `matched_source`。
  - Core 不给 Excel 增加 fuzzy 状态；Qt adapter 后置。

### SQLite 事务、journal 与备份

- **查阅来源**：
  - [SQLite transactions](https://www.sqlite.org/lang_transaction.html)
  - [SQLite WAL and WAL-reset advisory](https://www.sqlite.org/wal.html#the_wal_reset_bug)
  - [SQLite Backup API](https://www.sqlite.org/backup.html)
  - [SQLite atomic commit](https://www.sqlite.org/atomiccommit.html)
  - [Python sqlite3 transaction control](https://docs.python.org/3.14/library/sqlite3.html#transaction-control)
  - [Python Connection.backup](https://docs.python.org/3.14/library/sqlite3.html#sqlite3.Connection.backup)
- **发现**：
  - SQLite 同时只允许一个 writer；Python 3.14 推荐显式 `autocommit=False`、commit/rollback。
  - SQLite 官方于 2026-03 公布：3.7.0–3.51.2 的 WAL 在多连接同时 write/checkpoint 的紧密竞态下可能损坏，3.51.3 及部分 backport 修复。
  - 当前运行时实测 SQLite 3.51.2、FTS5 enabled。
  - `Connection.backup()` 可在数据库被访问时生成一致 snapshot。
  - SQLite 原子提交只覆盖单一数据库事务；canonical DB 与 JSONL/manifest 是多个文件，必须以 receipt + recovery 状态机处理崩溃窗口，不能假装一次 `os.replace` 能原子发布全部资产。
- **设计影响**：
  - 首版固定 rollback journal、`synchronous=FULL`、`foreign_keys=ON`、bounded busy timeout、每连接单线程所有权。
  - WAL 默认关闭；未来只有已知修复版本、序列化 writer 和并发/恢复测试同时通过才可启用。
  - schema upgrade 前使用 backup snapshot；首次 JSONL migration 使用 build-copy-validate-swap。

### FTS5 trigram 与候选索引

- **查阅来源**：
  - [SQLite FTS5 trigram tokenizer](https://www.sqlite.org/fts5.html#the_trigram_tokenizer)
  - [SQLite FTS5 external content](https://www.sqlite.org/fts5.html#external_content_tables)
  - [SQLite expression indexes](https://www.sqlite.org/expridx.html)
- **发现**：
  - trigram 支持 substring candidate recall，但 MATCH 查询少于 3 个 Unicode 字符不命中。
  - external-content FTS5 的一致性由应用负责，索引与表不一致会产生不可预测结果。
  - SQLite expression index 只接受 deterministic function；把运行时 Python Unicode case-fold 注册为 SQL deterministic 会把版本变化隐藏在索引后。
- **设计影响**：
  - 保存 `source_fold_v1`，不在 SQL expression index 调 Python case-fold。
  - 使用 `case_sensitive=1` 的 contentful FTS5 trigram fast path；预折叠文本不再叠加 SQLite 自己的大小写语义，也不采用 external-content/trigger 双写。
  - fuzzy 召回按 query unique trigrams 做 OR/overlap，不使用完整 query substring MATCH；短或退化 query 使用 1/2-gram posting fallback，无 FTS5 时使用 1/2/3-gram posting。
  - candidate budget 受版本化 contract 约束，并以 brute-force scorer oracle 的 above-threshold 与真实 top-10 100% recall 作为激活硬门。
  - FTS relevance 只负责候选，不成为 CAT final similarity。

### Unicode case、word boundary 与 normalization

- **查阅来源**：
  - [Python str.casefold](https://docs.python.org/3.14/library/stdtypes.html#str.casefold)
  - [Unicode UAX #29](https://www.unicode.org/reports/tr29/)
  - [Unicode UAX #15](https://www.unicode.org/reports/tr15/)
- **发现**：
  - case-fold 会产生长度扩展，例如 `ß → ss`。
  - UAX #29 word boundary 不等同于简单 `\b`；CJK 产品语义又需要显式连续匹配 tailoring。
  - 当前 Python `unicodedata` 为 UCD 16.0，而 Unicode 标准已演进；运行时升级可能改变类别。
- **设计影响**：
  - `text-v1` 固定 Unicode 16.0.0 fold projection 与 UAX #29 property/script snapshot；folded hit 映射为最小覆盖原文半开区间并去重。
  - pure CJK 明确限定 Han/Hiragana/Katakana/Hangul base 及其附着 marks；Whole Word 跳过额外边界过滤。
  - fuzzy normalization 使用 versioned NFC + casefold；exact 始终 raw；不使用会更改兼容字符含义的盲目 NFKC。

### Similarity scorer 与稳定排序

- **背景**：原 Feature 5 明确要求 Levenshtein 与 Dice。
- **评估**：
  - 仅用 Levenshtein 会丢失局部字符 bigram overlap 证据。
  - 仅用 Dice 会弱化编辑距离和短文本行为。
  - 可调权重在没有已批准 benchmark 语料时形成隐藏参数。
- **设计影响**：
  - `scorer-v1` 同时输出 normalized Levenshtein ratio 与 multiset character-bigram Dice。
  - final similarity 使用二者等权算术平均，避免未证实的偏置；两个分量继续随 evidence 返回。
  - scorer/version、threshold、candidate count 全部写入 benchmark report；未来调权必须升版本并重新验收。

### 多译文与 context 分类

- **发现**：需求要求保留同源多译文，但只要求旧兼容 winner 继续成为首个 exact。
- **设计影响**：
  - 最新 legacy/write record 是唯一 compatibility `EXACT` winner。
  - context-v1 只比较两侧非空的 raw speaker/prev/next 完整字符串；同 source 非 winner 只有至少一个字段明确相等时才返回为 `CONTEXT`。
  - 没有 context evidence 的其他变体继续保存/导出，但不伪装成 CONTEXT，也不打乱旧 exact 结果。
  - 非同源 candidate 即使 context 匹配仍是 FUZZY，context 只作 tie evidence。

## 架构方案评估

| 方案 | 优点 | 风险 / 限制 | 结论 |
|------|------|-------------|------|
| 单个全局 SQLite | 查询集中 | 资源隔离、删除、损坏和迁移故障域扩大 | 拒绝 |
| 每资源 SQLite sidecar | 与现有文件/状态模型一致 | 多资源查询需聚合 | 采用 |
| 当前运行时 WAL | 读写并发高 | 3.51.2 官方损坏竞态、checkpoint 复杂 | 首版拒绝 |
| rollback journal + 短事务 | 简单、durability 明确 | writer 会短暂阻塞 reader | 采用 |
| external-content FTS5 | 少复制正文 | trigger/一致性风险 | 拒绝 |
| contentful trigram + gram fallback | 召回快、覆盖短词/无 FTS | 索引体积增加 | 采用 |
| BM25 直接当 CAT score | 内置排序 | 语义与编辑相似度不等价 | 拒绝 |
| Levenshtein/Dice 等权 v1 | 两者都可解释、无隐藏权重 | 未来真实语料可能需要新版本 | 采用 |

## 设计决策

### 决策：每个旧 JSONL 对应一个 canonical sidecar

- **选定方案**：`name.jsonl` 的 deterministic store 为 `name.jsonl.sqlite3`；ResourceConfig 继续指向兼容入口，facade 优先使用已验证 sidecar。
- **迁移**：同目录 temporary DB 完整构建、校验后原子激活；旧 JSONL 永不删除或覆盖。
- **激活**：working stage 必须先完成 records、candidate indexes、integrity/FK/count/exact parity/source binding，再 close/fsync/seal；per-resource coordinator 只接受不可变 `SealedStage`，阻止新连接、排空 operation leases，执行 backup/replace/fsync/reopen health。
- **运行时权威**：首次 physical activation 前 JSONL 可作为 legacy store；成功后 SQLite 是唯一 exact/query/save 权威。fuzzy benchmark 或 matcher capability 未过只关闭对应能力，不回退 JSONL。
- **写入来源**：所有 canonical record 都引用通用 origin batch；migration/import batch 记录 source digest，本地 `save_record()` 使用单记录 `local_write` batch，并与 record/index 同事务提交。
- **导入协调**：sidecar 激活后，现有 `resource_importer.py` 必须把 incoming records 直接写成 canonical `import` batch；外部 JSONL 变化只产生 `SOURCE_DIVERGED`，不能静默替换已验证数据库。
- **快照同源性**：binding/receipt 记录 resource id、canonical store id、快照 digest 和其 canonical revision/high-water 基点。local write 只推进 canonical head，不修改 receipt；快照是同 canonical 的已知历史基点时仍属同源，不因内容落后而 divergence。
- **兼容导出**：按 record id 递增导出所有变体，确保重新迁移时最新 exact winner 仍在最后；stable read snapshot 生成 JSONL 和相邻只读 manifest，canonical ledger receipt 协调多个文件的发布与 crash recovery。
- **取舍**：UI 路径解释需在后续 adapter 中说明 canonical sidecar，但 Feature 5 Core 不拥有 UI。

### 决策：三套发布门彼此独立

- **Physical gate**：证明资源身份、来源绑定、schema、records、全部声明 candidate indexes、integrity/FK/count 和 exact parity 已闭合；成功后一次性发布 canonical generation 与 exact/save facade。
- **Retrieval correctness gate**：同 source raw context vectors 独立验证 CONTEXT；candidate/transaction/oracle 验证 FUZZY correctness，两者不互相冒充。
- **Fuzzy gate**：在 physical store 上以 oracle recall 与 `benchmark-v1` 分别验证 FTS5/fallback；未过时只关闭 FUZZY，不撤销 canonical 或已验证 CONTEXT。
- **Matcher gate**：独立 validation manifest 决定 `UNAVAILABLE / BASIC_VALIDATED / TEXT_V1_VALIDATED`；不得从 DB 可打开、FTS5 存在、测试文件存在或 Qt 本地推断升级。
- **理由**：数据权威、TM fuzzy 性能和通用文本匹配语义解决不同风险；混成一个 `ready` 会把性能失败误变成数据源回退，或把基础设施存在误报成语义已验证。

### 决策：TextMatcher 只通过 capability-gated port 发布

- **选定方案**：纯 `TextMatcherV1` 只供 Core 验证/执行；公开 `match(request)` 明确携带 legacy/basic/configurable profile，并返回 success/rejected 与同一次不可变 capability snapshot。
- **证据**：Core manifest 绑定 implementation artifact、semantics、fixture/cohort/evaluator digests 和有效期；公开 validation summary 只是 opaque safe digest。
- **状态**：BASIC 证据无效必为 UNAVAILABLE；BASIC 有效而 full 无效为 BASIC；同版本 BASIC+full 有效才为 TEXT_V1。
- **边界**：Qt、术语与 Legacy 不声明 readiness、不解析 summary、不选择 semantics version，也不在 Core 拒绝时伪造 fallback success。

### 决策：候选召回与最终评分严格分离

- **选定方案**：CandidateRetriever 只返回 record ids/recall evidence；RetrievalService 必须重新读取 canonical record 并运行两个 scorer。
- **理由**：索引更换不会改变 CAT score。
- **取舍**：多一步 record fetch，通过批量 SQL 控制成本。

### 决策：TextMatcher 使用版本化 Unicode 数据

- **选定方案**：生成并提交 `unicode_word_break_data.py`，`text-v1` 使用 fold projection 和 UAX #29 evaluator。
- **理由**：跨运行时可复验原始 offsets。
- **取舍**：生成数据文件需要更新流程；运行时无网络依赖。

### 决策：兼容 facade 后置激活

- **选定方案**：新 Core 模块先证明 exact parity 与 migration；之后才把 `TMEngine` 适配到 sidecar。
- **理由**：避免半完成 migration 影响 Qt/Excel。
- **取舍**：短期新旧查询路径并存，由 gate 决定切换。

### 决策：失败 outcome 与 benchmark 输入均版本化

- **失败契约**：migration/export/upgrade 分别返回 success/failure union；failure 冻结 stage、retryable、diagnostics、当前 generation 与原资产保持状态，不能依赖 UI 解析异常字符串。
- **性能契约**：`benchmark-v1` 固定 100k corpus/query/oracle digests、最低样本量、warmup、top-k、threshold、nearest-rank p95、child-process RSS scope 和全部硬门。
- **理由**：避免以缺失成功字段表示失败，也避免通过挑选查询或改变统计口径获得虚假性能通过。

## 风险与缓解

- JSONL malformed 行在旧 Engine 与 Controller 行为不一致 — migration preflight 明确 strictness profile，并报告每行。
- FTS5 availability 跨平台变化 — capability probe + gram fallback + report。
- gram index 体积与 migration 时间超限 — 分批 executemany、索引后建、100k gate。
- Unicode data 升级导致结果漂移 — semantics version、golden vectors、rebuild trigger。
- 多资源单次查询局部失败 — `QueryReport.resource_failures`，其余结果继续返回。
- sidecar/schema upgrade 损坏 — backup、transaction、integrity check、last-known-good activation。
- JSONL/manifest/DB receipt 跨文件发布中断 — issued receipt、digest reconciliation、可重放 manifest publication；canonical 不回滚。
- activation 同时切换 DB/manifest/binding — durable phased journal 同时备份并恢复 prior DB 与 prior manifest/binding，禁止只回滚一侧。
- 配置 JSONL 外部编辑与合法 canonical local write 混淆 — ancestry/binding 校验只核对快照资产，禁止当前内容等值比较。
- fallback 1/2/3-gram 在 100k 超限 — 独立 benchmark 报告并关闭该 fuzzy capability，不在 spec 阶段放宽门限。
- matcher 证据过期或实现漂移 — Core evaluator 自动降级，所有 outcome 携带同次 snapshot，消费方无 override。
- 当前 SQLite WAL bug — 默认 rollback journal，WAL capability 明确关闭。

## 参考

- [SQLite transactions](https://www.sqlite.org/lang_transaction.html)
- [SQLite WAL and WAL-reset advisory](https://www.sqlite.org/wal.html#the_wal_reset_bug)
- [SQLite FTS5](https://www.sqlite.org/fts5.html)
- [SQLite Backup API](https://www.sqlite.org/backup.html)
- [SQLite atomic commit](https://www.sqlite.org/atomiccommit.html)
- [SQLite expression indexes](https://www.sqlite.org/expridx.html)
- [Python sqlite3](https://docs.python.org/3.14/library/sqlite3.html)
- [Unicode Text Segmentation](https://www.unicode.org/reports/tr29/)
- [Unicode Normalization](https://www.unicode.org/reports/tr15/)
