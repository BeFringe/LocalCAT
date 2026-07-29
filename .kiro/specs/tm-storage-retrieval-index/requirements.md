# 需求文档

## 简介

LocalCAT 面向个人译者提供完全本地的翻译记忆库。当前能力只按 source 做精确查询，旧 JSONL 中的重复 source 采用最后写入胜出，既不能可靠保留同源多译文和上下文变体，也不能提供可解释的模糊建议。

本功能在不破坏现有精确查询、JSONL 数据、Excel 三态入口和 Qt 编辑闭环的前提下，使用户能够安全迁移本地 TM，获得按 exact → context → fuzzy 稳定排序的建议，并让下游 Qt 产品使用统一的 Match Case / Whole Word 匹配语义。

## 范围边界

- **范围内**：旧 TM 兼容与安全迁移、多译文和上下文变体、来源可追溯性、exact/context/fuzzy 检索、可解释模糊建议、统一文本搜索选项、本地隐私、失败恢复及十万条 TM 的可观察性能门。
- **范围外**：Qt 控件、菜单、快捷键、搜索导航和用户偏好持久化；机器翻译、语义向量、云端 TM、在线协作、账号与共享锁；项目文件解析和厂商 TMX 上下文映射。
- **相邻期望**：Qt 产品通过既有编辑协调入口消费建议和文本匹配结果；旧 Excel 工作流继续只看到 `TM_HIT / TERMS_FOUND / NO_MATCH`；未来 Parser 或 TMX 互操作变更只能触发兼容复验，不改变本规格的用户可见语义。

## 需求

### Requirement 1: 现有精确检索与入口兼容

**目标：** 作为现有 LocalCAT 用户，我希望升级后原有精确建议和 Excel 工作流保持不变，以便继续处理已有项目和资源。

#### Acceptance Criteria

1. The LocalCAT TM 子系统 shall 保持对 source 完整字符串的区分大小写、区分空白的精确匹配语义。
2. When 同一 source 在旧 JSONL 中存在多条有效记录, the LocalCAT TM 子系统 shall 继续把最后一条有效记录的 target 作为首个精确兼容结果。
3. When 查询命中精确记录, the LocalCAT TM 子系统 shall 返回类型 `EXACT`、100% 相似度以及与记录一致的 source 和 target。
4. While 某个 TM 资源未启用或未启用 Lookup, the LocalCAT TM 子系统 shall 不从该资源返回建议。
5. While 某个 TM 资源未启用或未启用 Update, the LocalCAT TM 子系统 shall 不向该资源写入确认译文。
6. When 相同输入通过旧 Excel 工作流查询, the LocalCAT TM 子系统 shall 保持现有 `TM_HIT / TERMS_FOUND / NO_MATCH` 状态及其既有字段含义。
7. When 旧 Excel 工作流未命中精确 TM, the LocalCAT TM 子系统 shall 保持先查询术语、再返回 `NO_MATCH` 的既有回退行为。
8. The LocalCAT TM 子系统 shall 不向旧 Excel 工作流引入隐式的第四种 context 或 fuzzy 状态。
9. When 一个既有 JSONL TM 资源完成迁移, the LocalCAT TM 子系统 shall 保持该资源对外可见的身份、配置入口以及 Active、Lookup、Update 选择不变。

### Requirement 2: 旧 TM 安全迁移与兼容导出

**目标：** 作为持有既有 TM 的译者，我希望迁移可预检、可核对、可重试，以便升级时不丢失可用翻译资产。

#### Acceptance Criteria

1. When 用户预检一个旧 JSONL TM, the LocalCAT TM 子系统 shall 报告有效记录数、无效记录数、重复 source 数和可保留变体数。
2. When 旧 JSONL TM 成功迁移, the LocalCAT TM 子系统 shall 报告迁移记录数、保留变体数、跳过记录数和每类问题的可定位信息。
3. When 迁移完成后查询旧有 exact source, the LocalCAT TM 子系统 shall 返回与迁移前相同的首个精确兼容 target。
4. If 迁移在解析、校验或写入期间失败, the LocalCAT TM 子系统 shall 保持原始 JSONL 字节和当前可用 TM 不变。
5. If 迁移失败, the LocalCAT TM 子系统 shall 返回可操作的失败原因，并允许用户在修正问题后安全重试。
6. When 用户重复执行同一迁移, the LocalCAT TM 子系统 shall 不静默创建额外的重复变体。
7. When 用户请求兼容导出, the LocalCAT TM 子系统 shall 生成可核对的本地 TM 数据，并报告导出记录数、跳过记录数和错误。
8. If 兼容导出失败, the LocalCAT TM 子系统 shall 不用不完整输出替换先前有效的目标文件。
9. When 迁移或本地 TM 存储升级报告成功, the LocalCAT TM 子系统 shall 只发布一个完整的活动 TM 版本，使全部已接受记录和该版本声明可用的查询能力同时就绪，并且不暴露部分准备的版本。
10. While 活动 TM 版本正在切换, the LocalCAT TM 子系统 shall 使每个查询或保存操作只观察到切换前或切换后的完整版本，而不观察到混合、部分或过渡性空版本。
11. If 新版本激活或重新载入失败且存在先前活动版本, the LocalCAT TM 子系统 shall 报告失败阶段并继续提供切换前的可用版本。
12. If 首次迁移的新版本激活或重新载入失败且不存在先前 canonical 版本, the LocalCAT TM 子系统 shall 报告失败阶段并继续提供原 JSONL 兼容能力。
13. If 原 JSONL 内容与当前活动 canonical TM 所绑定的来源不一致, the LocalCAT TM 子系统 shall 报告 `SOURCE_DIVERGED`、保持当前 canonical TM 为活动资源，并且既不得使用变化后的 JSONL 隐式替换或重建 canonical TM，也不得使用 canonical TM 隐式修改或替换发生分歧的原 JSONL。

### Requirement 3: 多译文、上下文与来源可追溯性

**目标：** 作为处理重复文本和角色对白的译者，我希望同源多译文及其上下文得到保留，以便判断哪条建议适合当前段落。

#### Acceptance Criteria

1. When 同一 source 存在多个有效 target, the LocalCAT TM 子系统 shall 把各个译文保留为可区分的变体，而不是静默折叠为一条记录。
2. When 建议来自可识别的资源或导入来源, the LocalCAT TM 子系统 shall 随建议返回足以区分该来源的 provenance。
3. Where TM 记录包含原始 speaker、前文或后文, the LocalCAT TM 子系统 shall 保留这些上下文事实供检索使用。
4. When 当前查询提供与记录可比较的上下文, the LocalCAT TM 子系统 shall 使用户能够区分 context 建议与普通 exact 或 fuzzy 建议。
5. If 当前查询或候选记录缺少部分上下文, the LocalCAT TM 子系统 shall 在不伪造缺失事实的情况下继续评估其他可用匹配类型。
6. The LocalCAT TM 子系统 shall 不使用 speaker 显示名、头像或其他纯展示配置改写原始匹配身份。
7. When 多个资源包含相同 source, the LocalCAT TM 子系统 shall 保留各自的资源身份和 provenance，使结果可独立追溯。

### Requirement 4: 稳定且可解释的检索顺序

**目标：** 作为译者，我希望建议顺序稳定且类型明确，以便优先使用最可靠的翻译。

#### Acceptance Criteria

1. When 一次查询同时产生多种匹配, the LocalCAT TM 子系统 shall 按 `EXACT → CONTEXT → FUZZY` 的类型顺序返回结果。
2. When 相同 TM 状态和相同查询重复执行, the LocalCAT TM 子系统 shall 返回相同的结果集合和稳定顺序。
3. When 返回任一建议, the LocalCAT TM 子系统 shall 提供匹配类型、相似度、target 和可用的 provenance。
4. When 查询指定最低相似度, the LocalCAT TM 子系统 shall 排除低于该阈值的 fuzzy 建议。
5. When 查询指定结果上限, the LocalCAT TM 子系统 shall 在保持类型优先级和稳定顺序的前提下限制结果数量。
6. If 没有任何结果满足查询条件, the LocalCAT TM 子系统 shall 返回明确的空结果，而不是把无关记录伪装为建议。
7. If 某个独立 TM 资源无法完成查询, the LocalCAT TM 子系统 shall 标识失败资源，并保留其他可用资源的结果。

### Requirement 5: 模糊建议的安全性与双 source 可见性

**目标：** 作为审阅模糊建议的译者，我希望看到建议为何匹配且由我决定是否应用，以免误用相似但不同的译文。

#### Acceptance Criteria

1. When 返回 fuzzy 建议, the LocalCAT TM 子系统 shall 同时暴露当前查询原文和 TM 中实际命中的原文。
2. When 返回 fuzzy 建议, the LocalCAT TM 子系统 shall 提供 0% 至 100% 范围内的相似度分数，且更高分表示更高文本相似度。
3. When 多个 fuzzy 建议具有不同分数, the LocalCAT TM 子系统 shall 先返回分数更高的建议。
4. When 多个 fuzzy 建议分数相同, the LocalCAT TM 子系统 shall 使用稳定且可重复验证的顺序返回它们。
5. The LocalCAT TM 子系统 shall 不自动把 fuzzy target 写入当前译文。
6. When 用户显式选择 fuzzy 建议, the LocalCAT TM 子系统 shall 只把所选 target 提供给现有编辑流程，且不自动确认当前段落。
7. If fuzzy 建议缺少实际命中原文、target 或有效分数, the LocalCAT TM 子系统 shall 不把该结果呈现为可应用建议。

### Requirement 6: Match Case 与 Whole Word 兼容语义

**目标：** 作为 Qt 搜索产品的使用者，我希望相同搜索选项在所有消费位置含义一致，以便获得可预测的命中范围。

#### Acceptance Criteria

1. When 调用方明确选择 Match Case, the LocalCAT 文本匹配能力 shall 只返回大小写完全一致的命中。
2. When 调用方关闭 Match Case, the LocalCAT 文本匹配能力 shall 按 Unicode 默认不区分大小写语义返回命中。
3. When 调用方关闭 Whole Word, the LocalCAT 文本匹配能力 shall 返回所有连续子串命中。
4. When 调用方启用 Whole Word 且查询不是纯 CJK, the LocalCAT 文本匹配能力 shall 只返回符合 Unicode 单词边界语义的命中。
5. When 调用方启用 Whole Word 且查询是纯 CJK, the LocalCAT 文本匹配能力 shall 不额外施加词界过滤，并返回与连续子串匹配相同的命中。
6. When 使用 legacy compatibility 选项, the LocalCAT 文本匹配能力 shall 复现“区分大小写 + 连续子串”的既有行为。
7. When 返回文本命中, the LocalCAT 文本匹配能力 shall 使用原始文本的起止位置，并按位置稳定排序。
8. If 查询为空或命中长度为零, the LocalCAT 文本匹配能力 shall 返回空结果。
9. The LocalCAT 文本匹配能力 shall 向 Qt 产品提供匹配结果和语义版本，但不规定 Qt 控件、默认选择、导航或偏好保存行为。
10. The LocalCAT 文本匹配能力 shall 不因 Match Case 或 Whole Word 选项改变 TM exact key 或术语记录本身的语义。

### Requirement 7: 本地隐私、资源隔离与失败恢复

**目标：** 作为重视隐私和资产安全的个人译者，我希望 TM 操作完全留在本机，并在局部故障时继续使用其余可用资源。

#### Acceptance Criteria

1. The LocalCAT TM 子系统 shall 在本机完成 TM 存储、迁移、检索、评分和兼容导出。
2. The LocalCAT TM 子系统 shall 不向网络发送 source、target、上下文、provenance、查询或性能样本。
3. The LocalCAT TM 子系统 shall 不要求账号、云端服务或遥测才能使用本规格能力。
4. If 一个 TM 资源损坏或不可读取, the LocalCAT TM 子系统 shall 报告具体资源和失败原因，而不静默丢弃错误。
5. If 资源更新后无法重新载入, the LocalCAT TM 子系统 shall 保留更新前最后一组可用查询结果来源。
6. If 向一个可写 TM 资源保存译文失败, the LocalCAT TM 子系统 shall 标识失败资源，并不得把该失败报告为成功写入。
7. While 多个 TM 资源同时启用, the LocalCAT TM 子系统 shall 隔离各资源的数据、错误和 provenance。
8. While 一个 `SOURCE_DIVERGED` 资源保持 Active 和 Lookup, the LocalCAT TM 子系统 shall 继续从其 last-known-good canonical TM 提供查询结果。
9. While 一个 `SOURCE_DIVERGED` 资源保持 Active 和 Update, the LocalCAT TM 子系统 shall 把确认译文写入其当前活动的 canonical TM。
10. When 确认译文成功写入一个 `SOURCE_DIVERGED` 资源, the LocalCAT TM 子系统 shall 保持该资源的 divergence 状态，直到显式消歧成功。
11. When 确认译文成功写入一个 `SOURCE_DIVERGED` 资源, the LocalCAT TM 子系统 shall 不修改发生分歧的原 JSONL。
12. When 用户或操作者显式请求 import 或 rebuild 且完整验证与激活均成功, the LocalCAT TM 子系统 shall 清除 `SOURCE_DIVERGED` 并报告新的活动版本。
13. If 显式 import 或 rebuild 未通过验证或激活, the LocalCAT TM 子系统 shall 保持 `SOURCE_DIVERGED`、last-known-good canonical TM 和发生分歧的原 JSONL 不变。
14. If 待激活 TM 的资源身份或来源绑定与配置资源不一致, the LocalCAT TM 子系统 shall 拒绝激活、标识受影响资源并保留先前可用版本。

### Requirement 8: 十万条 TM 性能门与可观察报告

**目标：** 作为评估升级可用性的操作者，我希望在明确记录的环境中验证大 TM 的查询、迁移和内存表现，以便发现不可接受的性能回归。

#### Acceptance Criteria

1. While 使用包含 100,000 条有效记录的验收 TM, the LocalCAT TM 子系统 shall 使预热后的 exact 查询达到 `p95 ≤ 50 ms`。
2. While 使用包含 100,000 条有效记录的验收 TM, the LocalCAT TM 子系统 shall 使 fuzzy top-10 查询达到 `p95 ≤ 500 ms`。
3. When 迁移包含 100,000 条有效记录的旧 TM, the LocalCAT TM 子系统 shall 在 `120 s` 内完成迁移并生成结果报告。
4. While 执行 100,000 条 TM 的查询与迁移验收, the LocalCAT TM 子系统 shall 把峰值常驻内存保持在 `512 MiB` 以内。
5. When 生成性能验收报告, the LocalCAT TM 子系统 shall 记录硬件、操作系统、运行环境、语料构成、查询数量、预热方式和统计口径。
6. When 生成性能验收报告, the LocalCAT TM 子系统 shall 分别报告 warm exact p95、fuzzy top-10 p95、迁移耗时和峰值常驻内存。
7. If 任一性能指标超过批准门限, the LocalCAT TM 子系统 shall 把验收标记为失败并指出超限指标。

### Requirement 9: 文本匹配能力的可验证发布门

**目标：** 作为消费文本匹配能力的产品，我希望获得由核心验证结果决定的唯一能力状态，以便只向用户开放已经证明语义正确的搜索选项。

#### Acceptance Criteria

1. The LocalCAT 文本匹配能力 shall 只报告 `UNAVAILABLE`、`BASIC_VALIDATED` 或 `TEXT_V1_VALIDATED` 三种能力状态之一。
2. When legacy compatibility 调用、`match_case=false` 且 `whole_word=false` 的 Unicode case-fold 连续搜索调用、case-fold expansion 后仍引用原文的稳定位置、空查询及对应基础黄金样例全部通过当前语义版本验证，但 `TEXT_V1_VALIDATED` 的完整验证证据尚未有效, the LocalCAT 文本匹配能力 shall 报告 `BASIC_VALIDATED`，并且只声明 legacy compatibility 与基础连续搜索两种调用用途可用。
3. If `TEXT_V1_VALIDATED` 的完整验证证据缺失、失败、过期或与当前语义版本不一致，但 `BASIC_VALIDATED` 的验证证据仍有效, the LocalCAT 文本匹配能力 shall 降级为 `BASIC_VALIDATED`，并停止声明可配置 Match Case 与 Whole Word 调用用途可用。
4. If 任一 `BASIC_VALIDATED` 验证证据缺失、失败、过期或与当前语义版本不一致, the LocalCAT 文本匹配能力 shall 报告 `UNAVAILABLE`，并且不得报告更高状态。
5. When `BASIC_VALIDATED` 验证证据仍有效，并且 Match Case 与 Whole Word 的四种组合以及 Unicode case-fold、Unicode word-boundary、数字、下划线、标点、混合脚本和纯 CJK 黄金样例全部通过同一当前语义版本验证, the LocalCAT 文本匹配能力 shall 报告 `TEXT_V1_VALIDATED`，并同时声明全部 `BASIC_VALIDATED` 调用用途、Match Case 与 Whole Word 调用用途可用。
6. While 能力状态为 `BASIC_VALIDATED`, the LocalCAT 文本匹配能力 shall 只接受明确声明为 legacy compatibility 或基础连续搜索的调用用途，并且不得把任意可配置 Match Case 或 Whole Word 调用用途标记为已经验证。
7. If 匹配请求声明的调用用途或选项不被当前有效能力状态覆盖, the LocalCAT 文本匹配能力 shall 拒绝请求、不得返回命中，并返回稳定且不包含 source、target 或 query 正文的原因。
8. When 成功返回文本匹配结果, the LocalCAT 文本匹配能力 shall 同时返回本次执行实际使用的能力状态和非空语义版本，并保证结果、状态和版本来自同一次核心能力判定。
9. When 返回任一可用能力状态, the LocalCAT 文本匹配能力 shall 同时返回可核对的验证摘要，并保证摘要与能力状态及语义版本来自同一次核心能力判定。
10. The LocalCAT 文本匹配能力 shall 只把验证摘要作为只读核对信息，并且不接受消费方从摘要推导的 readiness 作为能力判定、升级或降级的权威。
11. While 能力状态为 `UNAVAILABLE`, the LocalCAT 文本匹配能力 shall 拒绝所有文本匹配请求、不得返回命中，并返回稳定且不包含 source、target 或 query 正文的不可用原因。
12. The LocalCAT 文本匹配能力 shall 由同一核心验证证据和语义版本决定能力状态与调用用途覆盖范围，并且不把状态或用途的判定、升级或降级责任交给 Qt、术语表或其他消费方。
