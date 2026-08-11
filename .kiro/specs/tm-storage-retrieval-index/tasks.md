# 实施计划

- [x] 1. 冻结版本化基础契约与验证夹具

- [x] 1.1 定义记录、资源、查询、结果和失败契约
  - 固化记录身份、原始 source/target、上下文、来源批次、资源顺序、provenance、匹配类型和双 source 形状
  - 对相似度范围、非空身份、资源顺序、evidence 与匹配类型组合、局部失败安全摘要建立构造期校验
  - 完成时，合法契约可稳定往返，非法版本、非法范围、缺失 fuzzy evidence 或正文泄漏诊断均被拒绝
  - _Requirements: 1.3, 1.4, 1.5, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 4.3, 4.6, 4.7, 5.1, 5.2, 5.7, 7.4, 7.6, 7.7_

- [x] 1.2 定义 canonical、来源绑定和原子激活契约
  - 固化资源身份、deterministic sidecar、snapshot receipt/manifest、来源状态、阶段校验证据、sealed artifact、单次激活 token 与 generation
  - 区分 mutable stage 与 sealed artifact，禁止裸路径、可伪造 validated 标志或不匹配的资源/来源绑定进入激活
  - 完成时，只有身份、digest、ancestry、expected generation 和 artifact registry 全部闭合的不可变对象能构造激活请求
  - _Requirements: 1.9, 2.9, 2.10, 2.11, 2.12, 2.13, 7.5, 7.8, 7.9, 7.10, 7.11, 7.12, 7.13, 7.14_

- [x] 1.3 定义迁移、导出、升级和恢复结果契约
  - 固化预检计数、逐行安全诊断、成功证据、失败阶段、可重试性、资产保持证明和恢复路径
  - 以显式 success/failure 联合结果表达迁移、导出和 schema upgrade，禁止以半成品或空结果隐式表示成功
  - 完成时，每种成功与失败分支都能被类型化构造、持久比较，并证明原资产保持或提供 fail-stop 恢复位置
  - _Requirements: 2.1, 2.2, 2.4, 2.5, 2.6, 2.7, 2.8, 7.4, 7.5_

- [x] 1.4 定义文本匹配能力和不可变验证快照
  - 固化三态能力、legacy/basic/text-v1 用途、大小写与全词选项、稳定命中范围、语义版本、验证摘要和拒绝原因
  - 请求与结果绑定同一次不可变能力判定，消费方不能从摘要、SQLite、FTS5 或 benchmark 状态反推 readiness
  - 完成时，未覆盖用途、过期或版本不一致的证据会 fail-closed，公开拒绝信息不包含 query 或正文
  - _Requirements: 6.9, 6.10, 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.7, 9.8, 9.9, 9.10, 9.11, 9.12_

- [x] 1.5 固化 benchmark-v1 与候选元数据契约
  - 固化 100k 语料、查询 cohort、oracle、top-10、阈值、预热、p95、RSS、候选预算和四项硬门
  - 分离候选召回阶段计数与评分/过滤/global limit 后的资源结果计数，使 union、去重、截断、scored 和 returned 可对账
  - 完成时，缺少或篡改参数、计数不守恒、能力路径混报的报告都会被验证器拒绝
  - _Requirements: 4.2, 4.4, 4.5, 5.3, 5.4, 8.5, 8.6, 8.7_

- [x] 2. 实现无存储依赖的匹配与评分算法

- [x] 2.1 (P) 实现 similarity-v1 确定性评分
  - 实现 Levenshtein ratio 与多重集字符 bigram Dice，保留两个分项并以算术平均产生最终分数
  - 覆盖空串、单字符、重复字符、Unicode、阈值边界和重复执行稳定性
  - 完成时，版本化黄金样例的距离、gram 计数、两个分项和最终分数逐项一致
  - _Requirements: 4.2, 5.2, 5.3, 5.4_
  - _Boundary: SimilarityScorerV1_
  - _Depends: 1.1_

- [x] 2.2 (P) 实现固定版本 Unicode 折叠、词界与纯 CJK 分类
  - 使用固定语义版本的数据处理 case-fold expansion、原文 span 投影和 Unicode word-boundary，不依赖宿主正则词界
  - 纯 CJK 仅接受 Han、Hiragana、Katakana、Hangul；附着标记、ZWJ 和变体选择符只能跟随基字符
  - 完成时，数字、下划线、标点、组合字符、emoji、混合脚本和纯 CJK 分类黄金样例稳定通过
  - _Requirements: 6.1, 6.2, 6.4, 6.5, 6.7_
  - _Boundary: TextMatcherV1 Unicode Data_
  - _Depends: 1.4_

- [x] 2.3 实现 legacy、基础连续搜索与 text-v1 行为
  - legacy 保持区分大小写连续子串，基础搜索提供 Unicode case-fold 连续命中，text-v1 支持四种选项组合
  - Whole Word 对非纯 CJK 使用原文词界，对纯 CJK 明示退化为连续子串；空查询和零长命中返回空集
  - 合并折叠扩展造成的重复命中，并按原始文本起止位置稳定返回
  - 完成时，所有 profile 的命中集合和原文 offsets 与版本化黄金样例完全一致
  - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8, 6.9, 6.10_

- [x] 2.4 实现 matcher validation manifest 与三态证据评估
  - 以同一语义版本的基础 cohort 决定 UNAVAILABLE/BASIC_VALIDATED，以完整四组合与 Unicode cohort 决定 TEXT_V1_VALIDATED
  - 证据缺失、失败、过期、build/fixture/version 不一致时按规则降级，并发布不可变能力快照
  - manifest 显式绑定 evidence schema、artifact/build、semantics、cohort/fixture/evaluator digests、生成时间和有效期
  - 完成时，三态转换矩阵和 manifest 缺失/错配/过期测试全部通过，消费方无法设置或覆写状态
  - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.7, 9.8, 9.9, 9.10, 9.11, 9.12_

- [x] 2.5 实现 capability-gated matcher 执行端口
  - 每次请求只读取一次不可变能力快照，并用它校验 profile、选项和语义版本后执行对应匹配算法
  - 拒绝当前状态未覆盖的用途和选项且不返回命中；成功与拒绝都携带同一次判定的状态、版本和只读摘要
  - 完成时，profile×state×options、single-snapshot race 和无正文拒绝信息测试全部通过
  - _Requirements: 6.9, 6.10, 9.6, 9.7, 9.8, 9.9, 9.10, 9.11, 9.12_

- [x] 2.6 建立 Gate A 与 matcher 独立发布证据
  - Gate A 汇总契约、similarity 和纯文本算法的版本化 golden 结果；matcher gate 单独消费 matcher validation manifest
  - 证据失败只关闭对应算法或匹配用途，不从 SQLite、FTS5 或后续 benchmark 推断能力
  - 完成时，两道证据均可独立重算并阻止下游消费未验证契约或 matcher profile
  - _Requirements: 4.2, 6.10, 9.1, 9.2, 9.3, 9.4, 9.5, 9.9, 9.12_

- [x] 3. 建立事务化 SQLite canonical store

- [x] 3.1 创建 per-resource schema 与安全连接策略
  - 每个 TM 资源建立独立 canonical sidecar，包含 metadata、origin batch、完整 record、snapshot ledger/binding 和候选索引所需表
  - 建立 raw source exact B-tree、外键与版本字段，并启用 DELETE journal、FULL synchronous、foreign keys、5000 ms busy timeout
  - 显式记录运行时 SQLite/FTS5/Unicode 能力，保持扩展加载和 WAL 关闭
  - 完成时，schema、pragma、索引、外键、版本与运行时能力快照均通过自动检查
  - _Requirements: 1.1, 3.3, 7.1, 7.2, 7.3, 7.7_

- [x] 3.2 实现 raw exact、变体历史和事务化追加
  - exact 只按 source 原始字符串完全相等查询，并以最大有效 record identity 选择最后 winner
  - 按输入顺序追加 migration、local_write 或 import 批次，完整保存上下文、provenance、来源 ordinal 和预折叠文本
  - 为后续候选索引接入提供受控的同事务扩展边界，record/origin 写入任一阶段失败都整体回滚
  - 完成时，重复 source、多 target、record/origin 批次失败、并发 reader 和重开后的 exact winner 均保持兼容语义
  - _Requirements: 1.1, 1.2, 1.3, 1.5, 3.1, 3.2, 3.3, 7.6, 7.7_

- [x] 3.3 实现 generation lease 与资源隔离
  - 每次公开读写先取得当前 generation lease，再在线程内使用短连接；draining 后不发新 lease
  - 有界等待旧 lease 排空，阻止旧读者跨 generation 继续使用失效连接，并隔离各资源错误
  - 完成时，并发读写、busy timeout、drain timeout 和 generation 变化测试都只观察到一个完整版本
  - _Requirements: 2.10, 7.4, 7.5, 7.6, 7.7_

- [x] 3.4 实现 canonical revision、snapshot ledger 与来源状态机
  - 绑定配置 JSONL 路径、deterministic adjacent sidecar、manifest、snapshot receipt、canonical store identity 和 ancestry
  - canonical 正常写入只推进 revision 并形成 VERIFIED_HISTORY，不修改 snapshot、不触发或清除 divergence
  - identity、digest、manifest、ledger 或 ancestry 不一致时报告 SOURCE_DIVERGED，保持 last-known-good canonical 为读写权威
  - 完成时，VERIFIED_CURRENT、VERIFIED_HISTORY、SOURCE_DIVERGED 的转换和禁止双向隐式覆盖规则全部可验证
  - _Requirements: 1.9, 2.13, 7.8, 7.9, 7.10, 7.11, 7.12, 7.13_

- [x] 4. 建立可替换且可核对的候选索引

- [x] 4.1 实现 FTS5 trigram 快速召回
  - 对已由 fold-v1 生成的索引文本建立内容型 trigram，禁止再次折叠或把 tokenizer 大小写当作产品 Match Case
  - 为长度至少三的查询构造唯一 trigram 候选请求，并保持索引行与 canonical record 同事务
  - 完成时，预折叠输入、索引内容、回滚行为和候选集合通过确定性测试
  - _Requirements: 4.2, 5.3, 5.4_

- [x] 4.2 实现 1/2/3-gram fallback 与短查询召回
  - 一、二字符查询使用对应 posting；无 FTS5 时由 1/2/3-gram union 提供完整 fallback
  - 保持能力选择显式可诊断，并使相同配置的候选阶段顺序稳定
  - 完成时，短查询、纯 CJK 和禁用 FTS5 环境都能返回非空、可重复、受预算约束的候选集合
  - _Requirements: 4.2, 5.3, 5.4, 7.7_

- [x] 4.3 合并候选阶段、预算与召回证据
  - 对 overlap、union、去重、稳定预排序和截断逐阶段记录可守恒计数，输出索引路径与候选预算
  - 使用版本化预算限制候选，并保持召回层只返回候选身份，不承担最终 CAT 相似度排序
  - 把 canonical record、FTS 和 gram posting 写入接到同一事务，在每个索引阶段注入失败验证整体回滚
  - 完成时，两条路径的阶段元数据可完整对账，候选顺序不受底层 SQL 返回顺序影响，record/index 不会半提交
  - _Requirements: 4.2, 4.7, 5.3, 5.4, 7.4, 7.7, 8.7_

- [ ] 5. 实现迁移、封存、原子激活与恢复

- [x] 5.1 实现流式 JSONL 预检和幂等迁移计划
  - 在改动资源前计算 SHA-256，并报告有效、无效、重复 source、可保留变体及逐行安全诊断
  - 流式处理大型 JSONL，预先检查输入可读性、sidecar 条件、资源身份、目标可写性和已有 completed batch
  - 完成时，损坏行、摘要变化、不可写目标和重复同 digest 均在任何激活前得到确定结果
  - _Requirements: 2.1, 2.2, 2.5, 2.6, 7.14_

- [x] 5.2 构建完整 mutable stage
  - 在同目录新工作副本中按输入顺序写入 records、origin batches、全部候选索引、snapshot receipt 和 temporary manifest
  - 保留相同 source 的所有变体，不折叠或覆盖历史；同 digest 安全重试复用已完成结果
  - 完成时，尚未 sealed 的 stage 已具备记录/索引/receipt/manifest 全量内容，且记录数、顺序和逐字段内容与接受输入一致
  - _Requirements: 1.2, 1.9, 2.2, 2.3, 2.6, 3.1, 3.2, 3.3_

- [x] 5.3 校验、关闭并封存不可变 artifact
  - 在 mutable stage 上完成 integrity、foreign key、record/index count、exact parity、资源身份、来源绑定、版本和 digest 校验
  - 关闭全部连接并 fsync 数据库、temporary manifest 与 parent 后，才登记 opaque artifact 并产生单次 sealed stage
  - seal 后任何文件变化、registry/ref 不一致、错资源、错目标或 stale generation 均必须拒绝
  - 完成时，未闭合索引的工作副本无法 seal，已 seal artifact 无法继续写入或以裸路径激活
  - _Requirements: 2.3, 2.4, 2.9, 7.5, 7.14_

- [x] 5.4 建立 Gate B canonical physical readiness
  - 汇总 schema/runtime、迁移、完整候选索引、sealed evidence、来源绑定和 exact parity 证据
  - 未闭合索引、错 binding、无效 artifact 或 parity 失败都使 Gate B fail-closed，不允许进入激活
  - 完成时，Gate B 证据可独立重算，且只证明待激活版本完整，不提前发布 generation
  - _Requirements: 1.1, 1.2, 2.3, 2.9, 7.5, 7.14_

- [x] 5.5 实现唯一协调器的激活准备与排空
  - 仅在 Gate B 通过后接受登记且未消费的 sealed stage，并从证据生成绑定同一 artifact 的单次 token
  - 排空旧 lease，复核 prior generation、资源、目标、DB/manifest digest 和 source binding，并在修改资产前形成恢复备份
  - 完成时，token 重用、nonce 重放、过期 generation、错资源和 drain 失败都在 replace 前被拒绝
  - _Requirements: 2.9, 2.10, 2.11, 2.12, 7.5, 7.14_
  - _Depends: 5.4_

- [x] 5.6 实现 durable activation journal
  - 在任何替换前持久化 PREPARED，并为 DB_REPLACED、MANIFEST_PUBLISHED、GENERATION_PUBLISHED 定义单调阶段转换
  - 每个阶段落盘并 fsync，journal 与 token、nonce、artifact、new/prior receipt 和 manifest digest 必须一致
  - 完成时，重放、错配或已消费 token 无法推进 journal，正常路径留下可恢复的逐阶段证据
  - _Requirements: 2.4, 2.9, 2.10, 2.11, 2.12, 7.5_

- [x] 5.7 实现 DB/manifest 成套替换与 generation 发布
  - 只有 PREPARED 已持久化后才替换 DB、fsync parent、重开并校验 schema/digest/integrity/foreign key/count，再推进 DB_REPLACED
  - DB 验证后发布 receipt 与 manifest 并推进 MANIFEST_PUBLISHED，全部复核成功才发布 generation 并推进最终阶段
  - 完成时，并发查询和保存只观察切换前或切换后的完整版本，不出现空白、混合或过渡性版本
  - _Requirements: 2.9, 2.10, 2.11, 2.12, 7.5, 7.14_

- [x] 5.8 实现同一 activation token 的幂等完成恢复
  - 重启后复核新 DB、receipt、manifest、journal 与 token；只有全部匹配才从当前 phase 幂等继续
  - PREPARED 可安全取消，DB_REPLACED 可继续发布 manifest，MANIFEST_PUBLISHED 可发布唯一 generation
  - 完成时，各 phase 的同 token 重放只产生一个 generation，已完成 token 不可再次消费
  - _Requirements: 2.4, 2.9, 2.10, 2.11, 2.12, 7.5_

- [x] 5.9 实现不一致 activation 的成套回滚
  - 新资产任一复核失败时同时恢复 prior DB 与 prior manifest/binding，fsync parent 并重新执行健康校验
  - 首次激活失败且没有 prior canonical 时隔离未发布资产并继续原 JSONL；已有 canonical 时继续 last-known-good generation
  - 完成时，各 journal phase 的不一致注入都恢复一个完整可查询/可保存版本，DB 与 manifest 不会跨代
  - _Requirements: 2.4, 2.9, 2.10, 2.11, 2.12, 7.5_

- [x] 5.R1 收束 activation/recovery 模块边界
  - 在 5.9 闭合完整恢复矩阵后，将 journal/terminal codec、durable file protocol 与逐 phase completion/rollback 从 `tm_sqlite_store.py` 提取到设计指定模块；coordinator 只经窄 store-validation port 编排
  - 保持既有 `ResourceStoreCoordinator` 导入入口、journal phase、错误码、token/nonce 单次语义、fault-injection 顺序和 public lease/activation 行为，不夹带功能修改，也不拆分 `tm_contracts.py` 或 `tm_stage_sealer.py`
  - 用移动前后的同一 Cluster D characterization/failure matrix、二次冷启动、架构依赖守卫和全量回归证明等价；测试文件不设行数限制，已有 `test_tm_*` 继续作为行为权威
  - _Requirements: 2.4, 2.9, 2.10, 2.11, 2.12, 7.5, 7.14_
  - _Depends: 5.9_

- [x] 5.10 实现显式 import 与 rebuild 消歧
  - 已激活资源的显式 import/rebuild 通过 fresh mutable stage、完整索引、seal 与同一协调器切换
  - 仅完整验证和激活成功才清除 SOURCE_DIVERGED；失败保持 canonical、原 JSONL、manifest 与 divergence 不变
  - 完成时，成功消歧产生新 generation，失败路径不改变三方资产且 last-known-good canonical 继续服务
  - _Requirements: 2.13, 7.5, 7.12, 7.13, 7.14_

- [x] 5.11 实现 schema upgrade 的复制切换
  - 升级先创建一致快照备份，再在 fresh mutable copy 中迁移 schema、重建完整索引并复用 seal/activate
  - 不原地破坏唯一可用副本，保存升级前后版本、generation 与恢复证据
  - 完成时，成功升级产生等价新 generation，每个失败阶段都能重开旧 schema
  - _Requirements: 2.4, 2.9, 2.11, 7.5, 7.14_

- [x] 5.R2 收束 schema upgrade 模块边界
  - 在 Cluster E 行为与故障矩阵闭合后，将 v1→v2 copy 数据面、backup/locator pending→reported 持久化协议、strict locator file proof 与纯候选事实校验提取到设计指定模块
  - coordinator 继续独占 ticket/locator snapshot、lease/drain/state transition、activation guard 与 cold-recovery root 选择；`TMMigrationService` 继续编排公开 schema-upgrade 成败流程
  - 保留原导入入口、错误码、分支/cleanup/fault-injection 顺序和磁盘效果；不拆分 `tm_contracts.py` 或 `tm_stage_sealer.py`，不夹带异常分支简化或功能修改
  - 用移动前后的同一 Cluster E failure/interleaving matrix、public API 契约、依赖方向守卫、basedpyright 和 fresh 全量回归证明等价；测试文件不设行数限制
  - _Requirements: 2.4, 2.9, 2.11, 7.5, 7.14_
  - _Depends: 5.11_

- [x] 5.12 实现任意路径兼容 JSONL 导出
  - 按 canonical record identity 顺序向调用方选择的非配置路径导出完整 variants、上下文和 provenance，并生成 receipt 与 adjacent manifest
  - 使用 temporary file、file fsync、atomic replace、directory fsync、manifest publish 的显式协议
  - 不修改活动 snapshot binding、不清除 SOURCE_DIVERGED；导出目标后续损坏不影响活动资源状态
  - 完成时，export→migrate 的逐字段、变体和 exact winner parity 通过，报告绑定 canonical revision 与 snapshot receipt
  - _Requirements: 2.3, 2.7, 2.8, 2.13, 3.1, 3.2, 3.3, 7.10, 7.11_

- [x] 5.13 实现配置 JSONL 快照刷新发布
  - 只允许未 diverged 资源显式刷新配置 JSONL；先生成并验证 JSONL/manifest temporary pair，再提交 issued receipt
  - 按 JSONL replace、parent fsync、manifest replace、parent fsync、binding completed 的顺序发布
  - 完成时，成功刷新产生一致的 JSONL/manifest/ledger completed pair，且不改变 canonical records
  - _Requirements: 2.8, 2.13, 7.8, 7.9, 7.10, 7.11_

- [ ] 5.14 实现配置快照 refresh 崩溃恢复
  - issued receipt 对应旧 completed pair 时取消，JSONL 已替换但 manifest 未发布时由 ledger 重建 manifest
  - 未闭合 pair 不得报告成功；与 completed/issued ledger 均不一致时进入 SOURCE_DIVERGED，不回滚 canonical revision
  - 完成时，每个刷新阶段的失败注入都保持旧 completed pair、发布一致新 pair或明确进入 divergence
  - _Requirements: 2.8, 2.13, 7.8, 7.9, 7.10, 7.11_

- [ ] 6. 接入 physical canonical 与现有兼容入口

- [ ] 6.1 接入 canonical import seam 并保持资源配置身份
  - 已激活资源按验证后的输入顺序直接写入 canonical，不先修改 JSONL、不折叠相同 source；未激活资源保留既有原子 JSONL 路径
  - 普通 canonical merge import 不修改 snapshot binding，也不清除或触发 SOURCE_DIVERGED
  - 配置入口继续指向原 JSONL，sidecar 保持 deterministic adjacent path，Active/Lookup/Update 选择迁移前后不变
  - 不改变 Parser、TMX context mapping、Glossary 或 Qt 语义
  - 完成时，同一导入批次在两种激活状态下都保持顺序、资源身份、选择状态和明确回退行为
  - _Requirements: 1.2, 1.4, 1.5, 1.9, 3.1, 3.2, 3.7, 7.1, 7.2, 7.3, 7.7_

- [ ] 6.2 切换 Legacy exact 查询与受控保存
  - physical gate 与 exact parity 通过后，legacy exact/save 适配使用 canonical；未激活或首次迁移失败仍保持 JSONL 兼容
  - facade 不向旧调用约定注入 context/fuzzy；只有 Active+Update 且 generation 稳定时允许保存
  - SOURCE_DIVERGED 期间 Lookup/Update 继续使用 canonical，成功保存不修改 JSONL、不清除 divergence
  - 完成时，成功保存、拒绝保存、事务回滚、generation 变化和 divergence 矩阵的 exact winner 均符合预期
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 2.3, 2.11, 2.12, 2.13, 7.5, 7.6, 7.8, 7.9, 7.10, 7.11_

- [ ] 6.3 保持 LogicController 与 Excel 三态兼容
  - 保持 TM_HIT、TERMS_FOUND、NO_MATCH 三态和字段含义，不新增 context/fuzzy 第四态
  - 保持 exact 未命中后先查术语、再返回 NO_MATCH，以及既有资源优先级和 Excel 输出
  - 在不修改 Qt 控件或产品逻辑的前提下验证 sidecar 激活前后同输入 parity
  - 完成时，controller、Excel 与现有 Core 自检保持通过，不把 Qt journey 纳入本规格实现范围
  - _Requirements: 1.6, 1.7, 1.8, 6.10_

- [ ] 7. 实现确定性 exact、context 与 fuzzy 检索

- [ ] 7.1 实现 exact winner 与 raw context 分类
  - exact 仅使用 raw source 完全相等，winner 保持同资源最后有效记录；其他同 source 变体仅在存在正面 raw context 证据时分类为 CONTEXT
  - context-v1 只比较双方非空的 speaker/previous/next 原始完整字段，保持大小写和空白敏感，不伪造缺失事实
  - 完成时，EXACT→CONTEXT 类型、context strength 和 retained-only 变体黄金向量逐项一致
  - _Requirements: 1.1, 1.2, 1.3, 3.3, 3.4, 3.5, 3.6, 4.1, 4.2, 4.3_

- [ ] 7.2 实现 fuzzy 评分、阈值和显式选择安全
  - 从有界候选中批量读取 canonical records，使用两个分项和最终平均分，保留查询 source 与实际 matched source
  - 在排序和 limit 前应用 minimum similarity，按记录身份去重，并拒绝缺失 source/target/有效分数的建议
  - 查询只返回候选 target，不执行自动应用、确认或持久化副作用
  - 完成时，阈值边界、双 source、重复记录、limit 和无写副作用测试全部通过
  - _Requirements: 3.5, 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7_

- [ ] 7.3 聚合多资源稳定顺序与部分失败
  - 只为 Active+Lookup 资源获取查询 lease，Lookup 不授予写权限，Update 不授予查询权限
  - 按 EXACT、CONTEXT、FUZZY，最终分数、context strength、调用方资源顺序和稳定记录身份排序
  - 单资源失败不丢弃其他资源结果，并保留每条结果的资源、批次和 provenance
  - global limit 只在跨资源聚合后应用，最终资源元数据在评分/过滤/limit 后填入 scored 和 returned count
  - 完成时，置换底层执行顺序不改变结果、失败列表、provenance 或阶段计数
  - _Requirements: 1.4, 3.2, 3.7, 4.1, 4.2, 4.3, 4.5, 4.7, 5.3, 5.4, 7.4, 7.7_

- [ ] 7.4 建立 CONTEXT 与 FUZZY 独立可用性
  - physical canonical 激活只开放 exact/save；raw context correctness 证据单独开放 CONTEXT，oracle/benchmark 证据单独开放 FUZZY
  - 明确区分“能力可用但本次无命中”和“能力门未开放”，并提供稳定 unavailable code
  - CONTEXT 或 FUZZY 失败不得撤销 canonical authority、exact/save 或另一项已验证能力
  - 完成时，能力组合矩阵中的 QueryReport 和 health 状态与门禁证据一致
  - _Requirements: 4.6, 4.7, 7.4, 7.5, 7.7, 8.7_

- [ ] 7.5 建立 Gate C retrieval correctness
  - 分别汇总 raw context 分类、candidate 阶段计数、fuzzy 评分排序、事务回滚、局部失败和 global limit 证据
  - CONTEXT correctness 与 FUZZY oracle/benchmark 使用独立子门；未通过时只关闭对应查询类型
  - 完成时，Gate C 的每项证据可重算，且不会撤销 Gate B 已发布的 canonical exact/save
  - _Requirements: 3.4, 3.5, 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 7.4, 7.5, 8.7_

- [ ] 8. 建立 Gate D benchmark-v1

- [ ] 8.1 (P) 生成确定性 100k 与 oracle 语料
  - 固定种子生成 100000 条记录、至少 1000 个 exact 查询、至少 200 个 fuzzy 查询
  - 另生成固定 5000 条、200 查询的全扫描 oracle，阈值 0.60、limit 10
  - 完成时，相同版本与种子产生相同 corpus、cohort 和 oracle digest
  - _Requirements: 8.5, 8.6, 8.7_
  - _Boundary: TMBenchmark Corpus_
  - _Depends: 1.5, 2.1_

- [ ] 8.2 实现 exact/fuzzy 延迟测量运行器
  - 每个 cohort 预热 100 次，以 nearest-rank 计算 p95，并保留可重算的原始逐查询样本
  - 分别执行 FTS5 与 fallback，记录运行环境、能力、索引路径、查询数量和统计口径
  - 完成时，固定原始样本可重算 warm exact 与 fuzzy top-10 的 p50/p95/max
  - _Requirements: 8.1, 8.2, 8.5, 8.6_

- [ ] 8.3 实现隔离迁移与 RSS 测量运行器
  - 在独立子进程中测量从启动到完成的峰值 RSS，迁移计时包含 parse、insert、索引、校验、fsync、激活和 reopen
  - 排除预生成 fixture 成本，并记录两条索引路径的环境与原始样本
  - 完成时，固定样本可重算报告中的迁移耗时和峰值 RSS
  - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6_

- [ ] 8.4 实现全扫描 oracle 与召回硬门
  - 对阈值以上集合和真实 top-10 与固定全扫描 oracle 比较，逐查询报告缺失 candidate identity
  - FTS5 与 fallback 分别核对，任一遗漏都把对应 candidate recall gate 标记失败
  - 完成时，两条路径都有可重算的 recall 报告，只有 100% 才允许进入 fuzzy 性能发布门
  - _Requirements: 4.2, 5.3, 5.4, 8.7_

- [ ] 8.5 执行 fast/fallback 性能硬门并发布 Gate D
  - 两条路径分别验证 warm exact p95≤50 ms、fuzzy top-10 p95≤500 ms、迁移≤120 s、峰值 RSS≤512 MiB、recall=100%
  - 任一配置或指标失败时报告超限项并保持对应 FUZZY capability 关闭；成功路径不得掩盖失败路径
  - matcher BASIC/TEXT_V1 只按 matcher evidence 发布，不受 fuzzy benchmark 成败推断
  - 完成时，报告对每条索引路径和每个能力门给出独立 PASS/FAIL，且失败不撤销 canonical exact/save
  - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 9.1, 9.2, 9.5, 9.12_

- [ ] 9. 完成故障、边界与 86 条验收

- [ ] 9.1 执行迁移与激活故障矩阵
  - 覆盖损坏输入、record/index/commit/fsync 失败、并发 lease、busy timeout、token 重放及四个 journal phase 崩溃
  - 完成时，每个故障都有稳定证据，原 JSONL、last-known-good canonical 和 matching manifest/binding 按规则保持或成套恢复
  - _Requirements: 2.4, 2.5, 2.9, 2.10, 2.11, 2.12, 7.4, 7.5, 7.6, 7.14_

- [ ] 9.2 执行 snapshot 与 divergence 故障矩阵
  - 覆盖 export DB/JSONL/manifest crash、外部 JSONL 变化、正常 canonical 写入和 receipt/manifest/ledger/ancestry 错配
  - 覆盖显式 import/rebuild 成败、schema upgrade 失败和配置快照 refresh 恢复
  - 完成时，只有验证并激活成功的显式消歧会清除 divergence，其他路径均保持三方资产与 canonical authority
  - _Requirements: 2.4, 2.8, 2.13, 7.5, 7.8, 7.9, 7.10, 7.11, 7.12, 7.13, 7.14_

- [ ] 9.3 执行 matcher、context、fuzzy 与元数据证据矩阵
  - 覆盖 matcher 三态、证据过期/版本错配、用途×选项、single-snapshot race 和无正文诊断
  - 覆盖 same-source context vectors、candidate union/dedupe/truncate、global limit、部分资源失败和能力未开放
  - 完成时，matcher、CONTEXT、FUZZY 互不冒充，召回元数据与最终资源元数据逐阶段守恒
  - _Requirements: 3.4, 3.5, 4.1, 4.2, 4.4, 4.5, 4.6, 4.7, 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8, 6.9, 6.10, 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.7, 9.8, 9.9, 9.10, 9.11, 9.12_

- [ ] 9.4 执行兼容回归与架构守卫
  - 重新运行 legacy exact/save、资源优先级、LogicController、Excel formatter 与现有 Core 自检
  - 检查无网络、账号、telemetry、凭据或外部服务依赖，并验证每资源隔离、WAL/extension loading 关闭
  - 禁止 Core 导入 Qt 或改变 Parser/TMX/Glossary 职责
  - 检查 Qt、术语和 Legacy 层不能定义 matcher readiness、解析验证摘要或绕过 gated matcher
  - 完成时，既有回归零失败且所有依赖方向守卫通过
  - _Requirements: 1.1, 1.2, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9, 6.9, 6.10, 7.1, 7.2, 7.3, 7.4, 7.7, 9.10, 9.12_

- [ ] 9.5 对照全部验收标准完成发布门
  - 将 9 项需求的 86 条验收标准逐项关联到最新自动测试、故障证据、oracle 或 benchmark 报告
  - 任一证据缺失、失败、过期或版本不一致时保持对应能力未完成，不宣告 Feature GO
  - 完成时，86/86 覆盖矩阵均指向具体、可重算且版本一致的验证入口
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 2.10, 2.11, 2.12, 2.13, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8, 6.9, 6.10, 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7, 7.8, 7.9, 7.10, 7.11, 7.12, 7.13, 7.14, 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.7, 9.8, 9.9, 9.10, 9.11, 9.12_

- [ ] 9.6 执行完整发布验证
  - 重新执行核心套件、迁移/导出往返、激活恢复、matcher golden、candidate oracle、fast/fallback benchmark 和兼容回归
  - 核对所有任务勾选、阻断项、设计边界与跨组件集成，失败时保持相应 gate 关闭
  - 完成时，完整测试套件退出码为零、四道门证据为最新状态且不存在未解决阻断项
  - _Requirements: 2.9, 4.2, 6.10, 7.5, 8.7, 9.12_

## Implementation Notes

- 2026-07-29 / Task 1.1：冻结 `tm_contracts.py` 的记录、资源句柄、查询、结果、evidence、局部失败与严格 codec v1；运行时 `TMResourceHandle` 保持必需 `TMStore` 绑定且不进入持久 codec。独立复审通过；focused 15/15、全量 127/127、basedpyright 0 errors。
- 2026-07-29 / Task 1.2：冻结 canonical 资源身份、确定性 sidecar、snapshot receipt/manifest/binding、阶段校验证据与 generation 闭合；sealed artifact 和单次 token 保持 registry 背书的模块私有运行期能力，公开协调器只暴露 `activate(SealedStage)`。独立复审通过；focused 27/27、全量 138/138、basedpyright 0 errors。
- 2026-07-29 / Task 1.3：冻结迁移、导出与 schema upgrade 的显式成功/失败联合结果，使用 digest-backed 资产保持证据与无权限恢复定位替代可伪造布尔断言；strict codec 在编码与解码边界递归重验 lineage、计数和嵌套证据。独立复审通过；focused 38/38、全量 150/150、basedpyright 0 errors。
- 2026-07-29 / Task 1.4：冻结 matcher 三态 capability、用途/选项矩阵、请求摘要与无正文 outcome；公开 strict codec 与内部 validation manifest codec 分离，required cohort 与 evidence 精确闭合且消费方不能反推 readiness。独立复审通过；focused 30/30、全量 165/165、basedpyright 0 errors。
- 2026-07-29 / Task 1.5：冻结 candidate-budget-v1、召回/去重/截断/资源结果守恒与 benchmark-v1 双路径 suite；minimum 与实际 cohort count 分离，composition/digest/path/硬门均由 strict codec 闭合，真实 benchmark artifact 留给 Task 8.1 生成。独立复审第二轮通过；focused 32/32、全量 182/182、basedpyright 0 errors。
- 2026-07-29 / Task 2.1：实现纯 `SimilarityScorerV1`，固定 NFC→casefold、Unicode code-point Levenshtein、多重集字符 bigram Dice 与未舍入算术平均；版本化黄金向量逐项绑定 folded 文本、距离、gram 计数、分项和最终分数，不夹带召回、阈值、排序或存储行为。独立复审通过；focused 6/6（22 subtests）、全量 188/188（161 subtests）、basedpyright 0 errors。
- 2026-07-29 / Task 2.2：固定 Unicode 16.0.0/UAX #29 rev.45 属性表、来源摘要与运行时 fail-closed 闸门，实现带原文 provenance 的 whole-string NFC/casefold 投影、默认词界和严格 pure-CJK 分类；第一轮 canonical blocking 缺陷经独立复审拦截后改为标准 composition 状态机，并以完整 NormalizationTest 99,825 输入、WordBreakTest 1,826 向量和 scorer 回归闭合。独立复审第二轮通过；focused 15/15（1,890 subtests）、全量 197/197（2,029 subtests）、basedpyright 0 errors。
- 2026-07-29 / Task 2.3：实现 Core 内部纯 `TextMatcherV1`，固定 legacy、basic 与 configurable 四组合行为；overlap、NFC/casefold expansion、原文 span 投影去重、稳定排序、原文 UAX #29 Whole Word 过滤及 pure-CJK 连续匹配 tailoring 均由版本化 golden 闭合，且不携带 capability/readiness 权威。独立复审通过；focused 29/29（1,938 subtests）、全量 203/203（2,056 subtests）、basedpyright 0 errors。
- 2026-07-29 / Task 2.4：实现 Core 内部唯一 `MatcherCapabilityEvaluator` 与原子不可变快照 publisher，以独立 basic/full cohort 时间窗闭合 UNAVAILABLE、BASIC_VALIDATED、TEXT_V1_VALIDATED 三态；为忠实表达 full-only 过期降级，内部 evidence schema/manifest codec 升至 v2，旧 v1 严格拒绝且公开 matcher 契约形状不变。第一轮复审拦截多 full cohort 部分缺失误判 UNAVAILABLE 的缺陷，修复为“完整 BASIC + 任意 full 子集”降级语义并加入双 full cohort 矩阵；独立复审第二轮通过；focused 23/23（67 subtests）、全量 211/211（2,080 subtests）、basedpyright 0 errors。
- 2026-07-29 / Task 2.5：实现唯一公开 `CapabilityGatedTextMatcherV1` 执行端口，每次调用只读取一次不可变 capability 快照，复用冻结的用途/选项矩阵并将 success/rejection、请求摘要和同一快照成套返回；拒绝路径不执行算法且不泄露正文，授权路径只调用一次 `TextMatcherV1`。独立复审连续拦截 evaluator 语义重绑、发布窗口 TOCTOU 与 caller expectation ABA，最终改为 publisher 构造时深复制私有 expectation/evaluator，并在发布锁内二次核对身份、摘要及语义版本，任何漂移均 fail-closed 为 UNAVAILABLE。独立复审第四轮通过；matcher focused 45/45（1,998 subtests）、全量 219/219（2,116 subtests）、basedpyright 0 errors。
- 2026-08-01 / Task 2.6：建立可重算 Gate A 与独立 matcher release evidence；Gate A 机械盘点完整 `tm_contracts.__all__`、40 成员 codec union 与 `StoreHealth` 能力不变式，各组件输入缺失/畸形只撤销自身授权；matcher 的 BASIC 与 full-only cohort 原始 fixture 字节摘要、派生 transcript 和 UTC 时间窗独立闭合，full-only 缺失、畸形、空白篡改均只降级为 BASIC，BASIC/common evidence 失效才发布 UNAVAILABLE；CLI 按请求的 full/basic 层级 fail-closed。独立复审通过；focused 22/22、全量 248/248、basedpyright 0 errors。
- 2026-08-01 / Task 3.1：从旧会话未跟踪成果恢复并独立复审 per-resource SQLite schema 与安全连接策略；mutable stage 使用原子保留文件、严格身份/批准 schema digest/对象类型/索引/外键复核，固定 DELETE journal、FULL synchronous、foreign keys、5000 ms busy timeout，关闭 WAL 与扩展加载，并冻结 SQLite 3.51.2、FTS5、Unicode 16.0.0 运行时能力快照。独立复审通过；focused 17/17、全量 259/259（含 Qt smoke）、basedpyright error-level 0 errors。
- 2026-08-01 / Task 3.2：实现 raw exact winner、完整变体/上下文/provenance 历史以及 migration、local_write、import 的按序事务化追加；candidate 扩展先在事务外生成封闭计划，再由 store 在 origin/record/status/revision 同一事务内写入，真实 SQL、commit 与扩展失败均整体回滚。复审反复暴露的共同根因收束为 caller-owned 值闭包：所有 scalar/nested value 在 fold/hash/compare/connection 前做 exact-type 校验并复制，长期 store handle 重建私有 identity/stage/path 快照而不保留调用方引用；该经验已并入评审集群 v1。最终独立复审与定点复验通过；focused 35/35、全量 277/277（含 Qt smoke）、basedpyright error-level 0 errors。
- 2026-08-01 / Task 3.3：新增 per-resource `ResourceStoreCoordinator`，所有公开读写在打开线程内短连接前取得绑定 resource、generation 与私有 stage 快照的 operation lease；状态机在 DRAINING 后拒发新 lease，有界排空超时恢复 prior generation，旧连接关闭后才发布完整新 generation。SQLite busy/locked 归一为当前资源的可重试 lifecycle failure，另一资源保持可用；未提前实现 sealed activation、source binding 或 candidate retrieval。集群实现阶段机械验证通过；focused 39/39、全量 281/281（含 Qt smoke）、basedpyright error-level 0 errors，等待 Task 3.4 后执行 Cluster A 统一复审。
- 2026-08-01 / Task 3.4：实现 leased canonical revision、completed snapshot ledger 与 `SourceBindingMonitor`；严格核对 configured JSONL、deterministic manifest、receipt、resource/canonical identity、digest、record count 和 revision ancestry。正常 local/import append 只推进 canonical revision 并把 CURRENT 变为 HISTORY，不改双文件；任一外部或 ledger 错配锁存 DIVERGED，canonical exact/append 继续权威且修回文件或重开不会隐式清除。completed-binding seam 只登记已发布且与当前 revision 闭合的 pair，不实现 issued/temp/fsync/replace/recovery/clear。集群实现阶段机械验证通过；source/store focused 47/47、全量 289/289（含 Qt smoke）、basedpyright error-level 0 errors，进入 Cluster A 统一复审。
- 2026-08-01 / Cluster A 复审：xhigh 累积复审拦截 canonical facts/revision 的 autocommit 混读、drain 窗口 next stage 篡改后误发布，以及 HISTORY 只校验上界的 ancestry 缺口。修正把当前 schema 升至 pre-release v2，以唯一 `completed_revision` 让 batch completion、record count 与 head revision 同事务闭合；所有公开 revision/binding facts 使用单一 read snapshot，divergence latch 以 canonical fingerprint 重核并在事实变化时重试；generation 只在 drain 后重新完成 schema/identity/integrity/FK/ancestry health 验证才发布。原 reviewer 定点复验最终批准；cluster focused 51/51、fresh 全量 293/293（含 Qt smoke）、basedpyright error-level 0 errors。
- 2026-08-01 / Task 4.1：实现对已由 fold-v1 预折叠文本的 contentful FTS5 trigram 写入计划与 fast recall seam；长度至少三的查询按首次出现顺序生成唯一 code-point trigram，逐个作为转义 phrase 做 OR union，store 在 generation lease 内以参数绑定查询并返回去重稳定 identity。无 FTS 或短查询明确 unavailable，不伪造 gram fallback、budget、阶段 metadata 或最终评分；FTS 行继续通过 Task 3.2 controlled plan 与 origin/record/completed revision 同事务提交和回滚。集群实现阶段机械验证通过；candidate/store focused 47/47、全量 299/299（含 Qt smoke）、basedpyright error-level 0 errors，等待 4.2/4.3 后执行 Cluster B 统一复审。
- 2026-08-01 / Task 4.2：统一 candidate write plan 在 FTS 配置写入 FTS+唯一 1/2-gram、无 FTS 配置写入唯一 1/2/3-gram，继续由 store 同事务提交。1/2 字符查询只走对应 posting；无 FTS 长查询按 3→2→1 posting union 返回 matched/query overlap evidence，纯 CJK 与 SQL 行序扰动下保持 overlap 降序、record id tie 的确定顺序。caller limit 与 8192 candidate hard cap 共同限流，4096 posting cap 先形成实际执行集合，SQL 与 denominator 只使用同一集合；FTS 长查询显式留给 4.3 合并，不伪造 budget-v1 或阶段账本。集群实现阶段机械验证通过；candidate/store focused 57/57、全量 309/309（含 Qt smoke）、basedpyright error-level 0 errors，等待 4.3 后执行 Cluster B 统一复审。
- 2026-08-01 / Task 4.3：实现唯一 `CandidateRetriever`，在单一 generation lease + SQLite read snapshot 内按 FTS_TRIGRAM、按需 GRAM_2/GRAM_1，或无 FTS 的 GRAM_3/2/1 执行召回，并从同一 canonical `source_fold_v1` 快照重算 overlap 与长度差。完整构造 frozen `CandidateRetrievalReport`：source stage、UNION、DEDUPLICATE、可选 TRUNCATE 的计数连续守恒，`candidate-budget-v1` 只在 pool 超限时截断，候选按 overlap ratio、source length delta、record id 稳定预排并绑定真实 recall stages/rank；短 query 正确报告 GRAM_FALLBACK，不提前执行 scorer、threshold 或 global limit。集群实现阶段机械验证通过；candidate/store/contract focused 85/85、全量 320/320（含 Qt smoke）、basedpyright error-level 0 errors，进入 Cluster B 统一复审。
- 2026-08-09 / Task 5.1：实现流式 JSONL 预检，以输入摘要、资源身份、sidecar 前置条件和逐行安全诊断在写入前闭合迁移计划；损坏、摘要漂移、不可写目标与 completed batch 重试均得到确定结果，且诊断不回显正文。
- 2026-08-09 / Task 5.2：在同目录 fresh mutable stage 中按输入顺序写入完整 records、origin batch、候选索引、receipt 与 temporary manifest；保留同 source 的全部变体，并仅在可证明身份、摘要与完成事实一致时复用既有结果。
- 2026-08-09 / Task 5.3：以完整 schema/runtime、integrity、foreign key、record/index parity、identity、binding 与 digest 复核关闭 mutable stage；数据库、manifest 和 parent 的 durable boundary 闭合后才登记 opaque artifact 并签发单次 sealed stage，seal 后篡改、错资源或 stale generation 均 fail-closed。
- 2026-08-09 / Task 5.4：建立可独立重算的 Gate B，把 schema/runtime、迁移完成、候选索引、sealed evidence、source binding 与 exact parity 绑定到同一待激活 artifact；Gate B 只证明 physical readiness，不提前发布 generation。
- 2026-08-09 / Cluster C 复审：统一复审将 caller-owned mutable reference、stage 重用授权和 seal/Gate B 之间的 TOCTOU 收束为私有值闭包、registry-backed 单次能力与锁内重验；修正后 migration→seal→Gate B 流水线的 focused、fresh 全量及 basedpyright error-level 验证通过。
- 2026-08-09 / Task 5.5：在唯一 coordinator 中把 Gate B、sealed artifact、token/nonce、prior generation 与 source binding 闭合后才排空 lease；替换前重验候选 DB/manifest 并形成恢复资产，重放、错资源、过期 generation 与 drain 失败均止于 publication 之前。
- 2026-08-09 / Task 5.6：实现 write-once durable activation journal，PREPARED 先于任何替换落盘，随后只允许 DB_REPLACED→MANIFEST_PUBLISHED→GENERATION_PUBLISHED 单调推进；每阶段把 token、nonce、artifact、prior/new receipt 与 manifest digest 成套持久化并 fsync。
- 2026-08-09 / Task 5.7：实现 DB→manifest→generation 的分阶段原子发布；每次 replace 后均先完成 parent fsync 和对应资产复核才推进 journal，generation 只在新 DB、receipt、manifest 与 binding 全部一致时可见，并发 operation lease 只能观察切换前或切换后的完整版本。
- 2026-08-09 / Task 5.8：实现按同一 token 的冷启动幂等恢复矩阵；PREPARED 可安全取消，DB_REPLACED 可继续发布 manifest，MANIFEST_PUBLISHED 可发布唯一 generation，terminal replay 只确认既有完成事实，错配资产与已消费 token 均不能再推进。
- 2026-08-09 / Task 5.9：实现不一致 activation 的成套回滚和隔离；存在 prior canonical 时恢复 prior DB、manifest/binding 并重新验证 last-known-good generation，首次激活无 prior 时隔离未发布资产并保留 JSONL 路径，任何 phase 都不允许形成跨代组合。
- 2026-08-09 / Task 5.R1：在完整恢复矩阵闭合后，把 journal/terminal codec、durable file protocol 与逐 phase completion/rollback 从 `tm_sqlite_store.py` 提取到设计指定模块，coordinator 仅经窄 store-validation port 编排；119 个顶层定义完成等价迁移并保留原导入入口、错误码、fault injection 与单次 token 语义。
- 2026-08-09 / Cluster D 复审：累积复审进一步闭合 lease drain、single-link handoff、PREPARED cancellation/quarantine、backup cleanup、write-once marker 与 final/temp 严格状态矩阵；native xhigh 最终批准且无 P0–P3 遗留，activation 203/203、fresh 全量 627/627（skip 1，含 Qt smoke）、basedpyright error-level 0 errors。
- 2026-08-10 / Task 5.10：实现 `import_snapshot()` / `rebuild_from_snapshot()` 的显式消歧；每次请求以 fresh `import` origin、store id、snapshot id 和完整候选索引构建 sealed stage，由同一 coordinator 在 prior/candidate store-id 闭包下排空 lease、持久日志、切换并冷恢复。配置 JSONL 或相邻 manifest 缺失/改写只能由成功的全量激活清除 divergence；可证回滚失败在业务 API 返回前恢复 prior READY 权威，已持久 `GENERATION_PUBLISHED` 则拒绝回滚并向前恢复。同一 service 可对相同快照连续产生新 generation，canonical ledger/ancestry 破坏与外来 symlink/directory/multi-link 仍 fail-closed。集群实现阶段机械验证通过；focused 29/29、fresh 全量 656/656（skip 1，含 Qt smoke）、basedpyright error-level 0 errors，等待 Task 5.11 后执行 Cluster E 统一复审。
- 2026-08-10 / Task 5.11：实现保持 canonical store id 的 schema v1→v2 复制切换；coordinator 在 DRAINING 下闭合 active 多 revision ancestry、source binding 与资产身份，以 `Connection.backup()` 生成并 fsync 单次恢复快照和 opaque ticket，候选只从该快照构建。seal/Gate B 后的同一激活流水线在写 journal 前重验 ticket、generation、head revision 与 prior DB digest，备份后的并发写使陈旧候选止于 publication 前且可用 fresh ticket 重试。升级按严格 record-id/origin block 证明完成顺序，保留 records、variants、context、provenance、usage、origin/receipt 与 current/history binding，重建 gram/FTS；divergence、manifest/ledger 篡改和不可证明 ancestry 均不被隐式修复。失败按既有 cancellation/rollback/terminal recovery 恢复 prior READY 或返回诚实 UNVERIFIED 证据；复审修正把 5.10/5.11 共用 recovery locator 收紧为 no-follow、regular、single-link、digest 和终态 inode 复验协议，changed/unverified 若无旧字节副本则明确 fail-stop；schema 全量备份与 locator 采用 pending→reported 持久化生命周期，cold recovery 仅提升已完成升级的 backup 并严格清理未暴露 pending，已暴露稳定证据不被后续重试删除。Task 5.10 replacement 在同一 coordinator 锁内严格清理遗留 pending 并立即转入 DRAINING，防止 import 的 completed recovery 误提升旧 upgrade backup，活跃 ticket 不被清理且既有 lease 仍按原激活语义排空。Cluster E 累积复审最终批准且无 P0–P3 遗留；复审聚焦 267/267、fresh 全量 708/708（skip 1，含 Qt smoke）、py_compile 通过、Cluster E 变更文件 basedpyright error-level 0 errors。
- 2026-08-10 / Task 5.R2：将 schema-upgrade backup/locator pending→reported 协议、strict locator file proof 与 v1→v2 copy 数据面提取到 `tm_schema_upgrade.py`；`tm_sqlite_store.py` 减少 451 行、`tm_migration.py` 减少 373 行，新模块不反向导入两个 owner。coordinator 仍独占 ticket/locator snapshot、lease/drain/state/guard/cold-root，migration 仍独占公开成败编排；原 private patch seam 由 late-bound wrapper 保留，copy plan 每次调用重建并对 DDL/digest 做只读值快照，异常分支与 cleanup 顺序未改写。E-R `v4_flash_worker`/max 等价性复审最终批准且无 P0–P3 遗留；复审 focused/recovery 306/306、fresh 全量 718/718（skip 1，含 Qt smoke）、py_compile 通过、变更文件 basedpyright error-level 0 errors。
- 2026-08-10 / Task 5.12：在同一 lease 和 SQLite read snapshot 内捕获 canonical revision 与按 `record_id ASC` 排列的全部变体，导出固定字段顺序的 JSONL 与 adjacent manifest，并以 canonical ledger 的 issued→completed/cancelled 状态绑定 generation、revision、record count 和 receipt。发布使用排他 temp、file fsync、replace 前 digest+inode/缺席复验、atomic replace、parent fsync 与终态重验；普通写入/fsync/恢复拷贝失败只按已证明的创建 inode 清理，外来或无法证明的目标保持不删不覆盖并 fail-closed。空 provenance、完整 context/变体与 exact winner 往返保持，不改写 active binding、divergence、canonical records 或 generation。实施阶段 focused 203/203（skip 1）、basedpyright error-level 0 errors、py_compile 与 diff check 通过，等待 Task 5.13/5.14 后执行 Cluster F 统一复审。
- 2026-08-11 / Task 5.13：实现未漂移资源的配置 JSONL 快照刷新；稳定 canonical read snapshot 经 issued receipt、JSONL replace/fsync、manifest replace/fsync、严格 digest+inode 成对复核后，在单一事务完成 receipt 与 active binding，失败按已证明所有权恢复旧 pair 或保留可恢复证据。配置 pair 观察使用资源级可重入 gate 覆盖完整发布窗口，外部 monitor 与并发 refresh 只能观察前态或 completed 后态；symlink、multi-link、same-byte foreign inode 与不稳定身份均 fail-closed 并锁存真实 divergence，canonical records、revision 与 generation 不变。实施阶段 focused 151/151（skip 1）、basedpyright error-level 0 errors、py_compile 与 diff check 通过，等待 Task 5.14 后执行 Cluster F 统一复审。
