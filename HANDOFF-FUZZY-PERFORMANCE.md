# Windows Fuzzy 性能与可用性：评审交接

日期：2026-10-04。代码基线：`7b51cce583dbeb32d35047abbee614bbc609f457`。本临时分支只增加这份评审输入、数值摘录和离线复算脚本；不修改产品代码、正式规格、ADR、审批或验收状态，不构成性能修复或发行 PASS。

本文交接普通 frozen 的已完成能力、当前性能问题、已取得的证据和待评估的产品方案；阅读不需要先前会话或其他未公开草稿。原始报告留在本地；分享件仅使用**开发机 DEV**、**验证机 VAL**，不包含设备厂商/型号、CPU 型号、用户名、本机目录、主机名、地址或用户项目正文。本文是临时评审材料，不是长期规格或治理文件。

中段评审见 [REVIEW-FUZZY-PERFORMANCE.md](REVIEW-FUZZY-PERFORMANCE.md)；针对其中 A/B/C 的已有原件补证及轮询修复范围见 [SUPPLEMENT.md](review/fuzzy-performance/SUPPLEMENT.md)。原报告及本文基线不改签。

## 1. 要请外援回答什么

1. 同一普通 frozen 候选、同一固定 100k 基准，在两台 Windows 机器上为什么有约 1.65 倍的查询差异？当前定位只解释了主要算法成本，尚未解释机器间差异的因果关系。请区分事实、推测和需要的最小对照实验，不凭硬件名称或一次结果下结论。
2. Phase 1 对全部记录取得精确信息是否是可以简化的实现合同？如果优化，如何在保留召回、排序、快照与完整性语义下减少工作，而不是放宽 scorer、只取种子或隐藏遗漏？不要默认从“全量处理”直接跳到“多核化”。
3. “未达到发行性能目标”和“禁止用户使用 Fuzzy”是否应继续绑定？评估第 6 节的最小解耦方案及其剩余首次验证等待，不把数值失败改签 PASS。
4. 若 Fuzzy 按实际耗时稍后显示，现有日常查询与 Qt 线程接缝要怎样调整？后台 Gate 验证和日常查询是两条链；不得因为前者异步，就推定后者不会卡界面。

请给出有优先级的具体结论、精确 owner/合同影响和下一次有限实验的退出条件。无需提出完整硬件测评矩阵、重新引入 W3，或继续无目标地试参数。

## 2. 上次 REVIEW 之后的主要变化

上次评审是 [`8890813` 的 REVIEW-WINDOWS-FROZEN.md](https://github.com/BeFringe/LocalCAT/blob/8890813/REVIEW-WINDOWS-FROZEN.md)，它复核 `58292d7` 的 handoff。保护链 `6531b1e → 0dfe833 → 58292d7 → 8890813` 保持。`0dfe833` 仍是强证明 W3 转向普通 PyInstaller 的节点。

W3 是分叉前 Windows frozen 发行方案的工作标签：目标是让最终用户无需安装 Python/Qt，以 onedir/windowed 运行完整 LocalCAT；旧 ADR-022 同时把它绑定到定制 native entry、完整 Boot TCB、retained-source-only 执行及真实 packaged 业务验收。偏离开箱即用目标的是把整套强来源证明变成发行前置，而非自包含发行目标本身。continuation 保留这条强证明技术线；reassessment 在已修复基线上按 ADR-028 解除相应前置，继续追求同一个开箱即用目标，并保留数据保护、真实检索语义/性能验证与产品旅程。

上次结论：保留 source/数据保护/检索合同，普通发行采用 onedir/windowed、same-EXE workers、有限 owner 输入和既有 Host 发布生命周期；取消完整 native entry/Boot TCB/source-only 的默认前置；构建记录不能代替真实 Gate。先贯通真实生产链，再本机资格，再同候选产品旅程。

| 后续提交 | 已发生的能力或边界变化 | 证据范围与限制 |
| --- | --- | --- |
| `3e41130`、`077a0c4` | 正式 R10/R11/R12 及相邻 owner 消费规格收束；批准后移除旧 HANDOFF/REVIEW 的活动副本 | 原文仍可从 Git 获取；没有重写受保护链，也没有把临时 review 提升为规格 |
| `962f551` | 普通 EXE → owner 输入 → Matcher/Core → same-EXE migration/query → Host 发布/取消/退出贯通 | 平台 7.2a–c/7.2、Core 9.6e、Feature5 3.6b；小样本调用链不是正式性能资格 |
| `c7ecd55`、`054d488`、`5697aa7` | 区分验证完成与资格通过，保留恢复拒绝原因，让 oracle 验证响应关闭撤销 | 沿原 Core/Host 生命周期修补具体消费接缝，无第二套授权者 |
| `9110177`、`1195e31`、`07c9add`，及 `b8138d8` 至 `f08099b` | 数据保护/窗口入口/业务消费验证；修复建议应用的代次锁重入、POSIX 激活锁核验、深色预览、导出 TM 来源和激活游标释放 | 各提交保留其候选与 owner 范围；不可拼成未来候选整体 PASS |
| `42fc582`、`8f476d0` | 较早候选 `8faeee03e1dd` 的真实本机资格及 Core 产品消费完成并登记 | 平台 7.3 与 Core 9.6b 等历史验收有效；不代表本次 `7b51cce` 候选在另一台机器或整个发行范围通过 |
| `0c391f0`、`e8ecdf3`、`2368c76`、`47b4290` | 安全失败诊断、允许主动确认空译文、验证工作目录改用应用数据根、Windows 版本元数据 | 空译文问题已闭合；本次不再把它扩成性能研究 |
| `7b51cce` | gram 索引写入从逐条改为最多 128 行一批，保留原顺序和事务回滚；同步真实 Gate C 输入锚 | 优化的是迁移写入。dense 查询算法未改；不是通过调门限或缩小正式语料取巧 |

当前产品仍是 Windows 0.5.2 普通 frozen；source/轻 launcher 已完成。平台 7.4、8、9、10 的最终闭环未完成。较早候选完成的独立无用户安装 Python/Qt 启动、数据导入等验证有用，但本次候选仍须按变更范围及同候选原则闭合，不能宣布发行完成。

验证机已经收到并导入额外的 Fuzzy 测试 TMX，3010 条有效、0 条无效。它与下面的 100k 合成基准是不同输入；当前准入失败不是因为缺少这份用户数据。本资料不包含任何用户项目或 TMX 正文。

## 3. Phase 1 是什么，哪里存在成本

这里的 Phase 1 是**一次 Fuzzy 查询内部的 dense 检索第一阶段**，不是 Windows Task、构建阶段或 Gate C/D 的同义词。稀疏查询还存在 block 上界遍历路径，下面的 8 个诊断查询均走 dense。

当前 owning [Core Design：CandidateRetriever](.kiro/specs/tm-storage-retrieval-index/design.md#candidateretriever) 规定：

1. **Phase 1**：在只读事务内绑定当前资源、generation/head 与索引；读取全部记录的 folded 长度，取得 query bigram 与各记录的精确交集（无命中项为零），形成逐记录保守分数上界 U1。这里取的是紧凑事实，不是提前读取全部译文或对全部记录运行完整 scorer。
2. **建立候选前沿**：按上界逐步评分，取得真实第 k 名 K0；确定可安全排除集合 P1 与仍可能竞争集合 R。必须考虑原始 identity、同分顺序、阈值与 top-k，不能仅凭种子或某个分项分数断言没有遗漏。
3. **Phase 2 及后续评分**：只为 R 取得进一步文本投影并精化上界，继续排除和评分，最终交付完整结果。

因此“第一阶段的集合大于实际候选”指**待证明可能性的记录域大于最终需评分的集合**。问题在于不少排除发生在已支付全量精确交集与前沿构造成本之后。保留不漏结果的业务要求，不等于必须永久保留这一具体两阶段实现。

代码入口（均为本分支基线可读文件）：

| 位置 | 要检查的工作 |
| --- | --- |
| [tm_sqlite_candidate_projection.py](tm_sqlite_candidate_projection.py)，`candidate_proof_dense_phase1`（约 708 行） | query terms 与 gram postings 汇合、按 record 聚合交集、长度/ID/交集的 JSON 传输及校验 |
| [tm_sqlite_store.py](tm_sqlite_store.py)，`_candidate_proof_dense_phase1_body`（约 15825 行） | 事务、资源/generation/head/count/index binding、返回值验证与私有 receipt |
| [tm_candidate_index.py](tm_candidate_index.py)，`_load_dense_frontier`（约 1589 行）、`_refine_dense_frontier` | 遍历全域长度/交集、构造上界前沿、P1/R 分区、后续精化 |
| [tm_retrieval.py](tm_retrieval.py)，`prove_and_score_fuzzy_candidates` | 生产 scorer、完整性与最终结果；不能用诊断捷径代替 |

仅在**开发机 source** 做过 8 个指定查询的成本插桩：

| query_id | 未插桩总耗时 ms | 插桩总耗时 ms | 候选前沿累计 ms | 实际 scorer 次数 | Phase 1 后 P1 排除条数 |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 110 | 366.36 | 531.46 | 400.14 | 19 | 97585 |
| 183 | 369.92 | 649.05 | 439.54 | 33 | 94616 |
| 194 | 454.89 | 693.11 | 462.54 | 32 | 93509 |
| 145 | 381.67 | 652.26 | 460.59 | 42 | 96917 |
| 157 | 359.93 | 568.47 | 411.21 | 33 | 97113 |
| 169 | 341.76 | 590.54 | 399.96 | 42 | 96700 |
| 181 | 395.64 | 647.77 | 452.39 | 33 | 95257 |
| 193 | 427.75 | 707.24 | 472.37 | 35 | 93855 |

这 8 项全部检查 100000 identity，结果与未插桩运行相同、保留工件前后不变。前沿构造占插桩总耗时 66.73%–75.29%；projection 部分同时包含 SQL、JSON 解码和验证，不能全归为 SQL 时间。累计时间互相嵌套，不可相加。source fingerprint 与 packaged fingerprint 不同；这是定位信息，**不是两机 Phase 1 耗时对照，也不是正式 Gate 样本**。

## 4. 两台机器的正式候选结果

为免依赖旧会话的实验编号，本文统一称下面这次为 **CANDIDATE**，后面的调度实验为 **QOS**。两者不可混成一次验收。

CANDIDATE：同一个 stock PyInstaller onedir/windowed 产物，代码 `7b51cce`；两机使用包内 CPython 3.14.7、SQLite 3.50.4、Unicode 16.0.0，Windows x64。开发机有开发环境，验证机不安装用户 Python/Qt；真实产品运行使用包内 runtime。硬件、系统配置与运行负载不是严格控制变量；不从设备型号推断速度。

候选 ID：`2addbf93a1287a11599ba4fcbb31456c909b13d01f5ea37eba118526baaf54c0`。Core fingerprint：`1735533d07b1c5f185270a89c9cbb1e25699ed89ec2ed0460839b55ccd67f000`。

真实正常入口发起 Core owner Gate；两条 intended path 各有独立 migration/query 进程、100000 条固定合成记录、1200 exact/240 fuzzy 样本及 5000 条/200 查询 oracle。每 cohort 预热 100 次，正式测量 1 轮，nearest-rank P95，top-10、阈值 0.6；计时为 `perf_counter_ns`，RSS 保留原 child lifetime 口径。使用仓库 [benchmark 合同](benchmark_tm_contract.json) 与 [tm_benchmark.py](tm_benchmark.py) 固定 seed `20260729`；并非多轮取最快值。两机分别迁移建立 DB：语料与查询相同，不宣称 DB 文件逐字节相同，也尚无两机配对的 SQL plan、分阶段计时及缓存/磁盘条件记录。

| 机器/路径 | Fuzzy P50 ms | Fuzzy P95 ms | Exact P95 ms | 迁移 s | 峰值 RSS MiB | 召回 | 原报告 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| DEV / FTS5 | 200.9187 | 390.8631 | 1.1471 | 70.2095 | 413.8633 | 1.0 | PASS |
| DEV / fallback | 207.0786 | 392.0292 | 0.7027 | 102.5577 | 418.7852 | 1.0 | PASS |
| VAL / FTS5 | 348.6263 | 644.1111 | 1.8019 | 100.4750 | 415.2969 | 1.0 | `FUZZY_P95` 失败 |
| VAL / fallback | 344.9478 | 641.6725 | 1.7446 | 147.8520 | 419.2813 | 1.0 | `FUZZY_P95`、`MIGRATION` 失败 |

VAL 两路 oracle 的阈值以上遗漏、top-10 遗漏、index-kind drift 均为零。失败报告真实保存，冷恢复仍拒绝 Fuzzy，报告/资格文件未因重启改变；开发机资格可冷恢复。**这些事实说明当前拒绝主要来自时间门，不证明验证机发行性能已通过。**

附带 [query-samples.csv](review/fuzzy-performance/query-samples.csv) 保留两机四组各 240 个逐查询纳秒值。以相同 query_id 配对，VAL/DEV 耗时比的中位数为 FTS5 **1.667549**、fallback **1.652047**；近似匹配与 miss 查询都普遍变慢，不只是少数尾部离群。这个现象支持继续检查公共查询路径与运行条件，不能单凭比例认定 CPU 或算法就是全部原因。

### 限定调度诊断 QOS

使用较早的 `47b4290` 普通候选及其真实迁移留下的同一 100k DB，固定请求、查询顺序、DB 字节与 Normal priority；同一验证机依次运行 默认 → HighQoS → 默认，每次 fresh same-EXE query worker。仅中间 worker 的 execution-speed QoS 从 system-managed `0/0` 改为 `1/0` 并读回，没有更改系统电源计划、亲和性、杀毒设置或全局优先级。

这不是 CANDIDATE 的再次验收。`47b4290..7b51cce` 的相关算法差异仅是 gram 写入批量化，既有 DB 上的 dense 查询函数未变；但候选、兼容身份、DB 构建时机和运行状态仍不同，不能与正式数值拼接宣称改进。

| 顺序 | Fuzzy P50 ms | Fuzzy P95 ms | 超过 500ms / 240 | worker CPU s | worker 墙钟 s |
| --- | ---: | ---: | ---: | ---: | ---: |
| 默认 1 | 311.7593 | 562.6560 | 42 | 110.1875 | 101.4764 |
| HighQoS | 313.1365 | 555.3499 | 37 | 110.1250 | 100.2142 |
| 默认 2 | 317.8808 | 591.9813 | 40 | 112.6094 | 101.7577 |

按 query_id 配对，中间运行/前后默认均值的中位比为 **0.990092296**，约快 1%；默认 2/默认 1 中位比 **1.007288372**。CPU/墙钟约 1.09–1.11，表示整批平均约一个逻辑核，不表示任何瞬间都严格单线程。三次均插电、节电模式关闭；DB 和请求前后字节不变，严格 Core codec 与进程/请求配对已在本地复核。

结论仅是：**HighQoS 没有表现出足够收益，停止该调度方向**。不能由此排除有效频率、热状态、存储、缓存、内存或其他进程影响。正式运行的 `_Total` 系统频率计数不是活跃核心的有效时钟；两机没有匹配的核心级测量、冷暖缓存控制或重复交叉运行，这些因果信息仍缺。

曾有局部连接参数、Python 聚合、covering index、mmap、seed/分块成本探针；没有得到已批准的生产查询修复。它们使用的阶段、DB、插桩和候选不一，本分享件不收录全量原始数值，故**不把这些尝试当作外援必须接受的排除结论**。若建议重新试某一项，请先解释新假设和为何足以改变总成本判断，避免重复参数遍历。

### 摘录的自包含与限度

- [query-samples.csv](review/fuzzy-performance/query-samples.csv)：四组正式候选与三组 QOS，共 1680 个原始 `elapsed_ns`；只选取 experiment/machine/path/query_id/elapsed_ns，未重算、重排或替换样本值。机器和实验名称作了匿名映射。
- [phase1-profile.csv](review/fuzzy-performance/phase1-profile.csv)：8 项 source 定位的计时、集合大小和结果相等标记，未携带用户文本或本机路径。
- [summarize.py](review/fuzzy-performance/summarize.py)：标准库离线复算 P50/P95、配对比值及插桩占比，不导入产品，不签发 Gate。
- 数值足以独立检查本文统计与定位论证；摘录刻意不含完整环境 envelope、HMAC、设备密钥、日志或用户数据，**不是可供产品恢复的资格 bundle，也不能独立证明真实执行来源**。
- DEV 原报告：`9d2e91c68874a6d8f8c28436a1fb3f09bbdec9a1ecef2d6cd3c298f3689df0a5`；VAL 原报告：`87a4b394d343a0805745ac0f9f0a12bb91085bae223e5c517570bc3f8c730f90`。摘要只是定位原件，不以摘要代替来源证明。

离线复算：`python review/fuzzy-performance/summarize.py`。query_id 1–200 为 near-edit，201–240 为 miss；原查询可由 `tm_benchmark.iter_fuzzy_queries(seed=20260729, record_count=100000, cohort_count=240)` 再生成。需要实际重跑时，在独立干净 checkout 的 `7b51cce` 按 `requirements-frozen-build.txt` 和 `python tools/build_windows_ordinary.py --output <隔离输出目录>` 生成新候选，通过正常产品入口显式发起验证；新候选/环境的结果必须单独标识，不能覆盖本文事实。这里只提供复现入口，本轮不启动新 Gate。

## 5. 算法方向：待评估，不是已选定修复

需要评估的唯一结构假设是：**能否用覆盖全部未评分 identity 的便宜保守上界，先排除一部分记录，再按需取得精确交集？** 当前 block maxima 已在这些查询上退化，简单再套一层现有 maxima 不足以解决。

- 潜在最小改动落在 Core 私有 phase-1 facts/receipt、前沿与 U1→P1/R 分区及守恒 metadata；对应 Core Design CandidateRetriever/性能段、Tasks 8.6–8.7。这可能需要修订 owning Design，不能包装成不改变合同的 SQL 微调。
- 保留 scorer/fold、threshold/top-k、raw-distinct identity 与同分顺序、generation/head 快照、完整性与独立复算、scorer budget、数据保护。是否必须继续取得全部精确 I 是实现问题，不先当作永久红线。
- 进入产品实现前先说明覆盖/闭合论证，并用一个有限原型证明总成本下降空间。若无法覆盖遗漏项、仍需接近全量 postings，或新增验证/迁移/内存成本抵消收益，就结束这个假设。
- 多核不是免费开关：SQLite query connection 当前请求 `PRAGMA threads=1`，它只是辅助 worker 上限；若提议并行，需说明跨任务快照、排序、取消、内存与合并成本，不默认承诺新调度框架。

## 6. 产品议案：性能结果与使用许可分开

### 6.1 待评估的最小解耦方案

建议不再由固定 100k 基准的**时间指标超限**单独否决本机 Fuzzy。它是共享 Core 的规则，覆盖 source 与普通 frozen，不是在 Windows UI 加一个绕过开关；受影响的其他 source 平台需按原 owner 回归，不引入 Mac frozen 计划。该方案尚未批准。

| 状态 | 建议行为 |
| --- | --- |
| Gate C、实际路径 oracle/召回、执行来源或兼容性验证缺失/失败 | 继续不可用；UI、构建清单和自洽 JSON 均不能授予资格 |
| 真实有效的本机完整报告仅 `EXACT_P95`、`FUZZY_P95`、`MIGRATION` 时间项超限 | Core 可发布 Fuzzy 可用；保留性能失败报告，向用户说明本机性能不足 |
| RSS 超限、工件漂移、没有完整有效执行结果等其他现有阻断 | 最小方案保留现行拒绝；不是豁免全部 Gate |
| 单次请求因预算、完整性、取消、失配或资源故障未完成 | 保留请求/资源级失败，不显示不完整或过时建议、不自动应用译文 |

500ms、120s、50ms、512MiB 及原始样本不改；原报告 `passed=false` 和失败项不改。**解除的是时间结果对功能准入的一票否决，不是把 Gate C 与 Fuzzy 语义解耦。** Gate C 仍验证 scorer/语义；Gate D 同时包含实际路径 oracle/召回和性能，不能整个删除。

这一最小方案仍要求首次/实现失配后显式跑完本机完整验证，不能解决第一次约十余分钟的等待，也不能把开发机资格搬到验证机。请外援明确评估它是否仍保留了不必要的用户负担。如果提议把本机完整 100k 验证也移到研发/发行环节，须另列对 ADR-013 的更大取代范围，说明实际路径语义、兼容性、设备恢复及旧资格如何处理；不能暗含在“仅时间超限可用”里。

### 6.2 按实际耗时显示建议的交互方向

拟议交互是：Fuzzy 建议可以慢一些，按真实查询完成时间出现。TM 面板渐进显示，而非每次慢查询弹出模态警告；等待期间编辑可继续，必要时显示“查询中”，完成后显示当前请求的建议及可选耗时。快速切段或修改查询时，旧结果必须失效；不能因为改成异步就展示错段建议。

**当前存在具体接缝**：[qt_editor_window.py](qt_editor_window.py) `refresh_suggestions()` 在没有可复用报告时直接调用 [editor_controller.py](editor_controller.py) `tm_suggestion_report()`，后者持 query lock 同步调用 `_query_and_issue_current_tm_report()`。这条日常建议路径不因 Gate D 使用后台 worker 而自动异步。只放开准入、只改文案，不能保证“慢但不阻塞”。应由 Feature5/Qt owning Design 明确后台查询、query epoch/generation/项目与段身份、结果应用和取消/关闭；已有身份应优先复用，不新建平行 authority。

不在本议案凭空规定“1 秒/5 秒就永久禁用整机”。发行基准门限、一次请求的交互等待/取消、结果正确性是三个不同决定；如需请求超时，应有可观察依据与重试/取消语义，不用它重造设备性能准入硬门。

### 6.3 owner 与批准范围

1. 治理：以新 ADR 精确取代 ADR-013 的时间硬门准入依赖；保留 Core 唯一 authority、设备内恢复、兼容性及非时间失败范围，同步 ADR 索引、tech 与 delivery-boundaries。若选择更大方案，单独写明哪些首次验证/本机资格前提被取代。治理按既有专门分支共享 tip 规则处理，不静默改原 ADR。
2. Core：Requirements 8 及 capability evaluator/发布/恢复 Design 和 Tasks 区分性能 verdict 与可用性；继续严格重算报告，使用真实 receipt、publisher 和兼容身份。不要新增 profile 或 UI 旁路授权。
3. Feature5/Qt：安全投影分别表达进度、可用性、性能结果；评估 6.2 的日常查询异步接缝，保留当前请求身份、取消与关闭、冷恢复。UI 不读取原始 bundle 自行判断授权。
4. 平台：R12/最终任务引用 Core 的明确裁决；同候选旅程、干净环境、数据保护、真实 C/D、100k 双路径与受影响 owner 回归仍按批准范围执行。

**发行边界不自动改变。** 功能可用不等于性能目标或 0.5.2 发行完成；现有最终验收仍未通过。若要在目标环境时间超限时发行，应明确受支持环境、接受的限制和发行标准，不能倒挑一台通过机器，或把交互成功当作性能 PASS。

正式 R/D/T 获批前不实施上述变更。若采用最小方案，后续按一个完整能力组织 Core/Host/Qt 与冷恢复验证，覆盖“仅时间失败仍可用且显示失败”“其他必要失败继续拒绝”“取消无晚到授权”“冷恢复同义”；再在同一最终候选完成真实建议、应用、保存重开。若同步采用异步建议，须增加当前请求/切段/关闭反例，而不是零碎追加状态补丁。

## 7. 单独文档审计：避免让陈旧描述继续驱动实现

本节只记录本轮只读质量检查，不提出新的构建或验收门。

- **owner 驱动的 frozen 构建清单**：`8e054db` 对应旧 W3 7.0/7.1。其父版本的 Tasks 与 ADR-022 已要求 owner roots、递归闭包、source-only；不是普通路线突然增加的要求。owner 驱动是从 Core 的 Gate roots/benchmark/dynamic imports/worker 声明取输入，避免 packaging 手写第二套业务清单。普通 [build_windows_ordinary.py](tools/build_windows_ordinary.py) 只复用已有声明及实际 Analysis/PYZ/data 对应，**不调用旧完整 AST 清单生成器**。
- **实际陈旧点**：[平台 Design](.kiro/specs/windows-platform-enablement/design.md) 约 114–117 行架构图、155–162 行文件计划仍写旧生成器和 `packaging/windows/LocalCAT.spec`。实际普通脚本在输出目录生成 spec，使用根目录 `requirements-frozen-build.txt`。`3e41130` 修改普通说明时留下旧节点，`962f551` 实现后未同步；应修正文档，不反过来为旧图增加产品依赖。
- **Historical / Current**：[cross-spec-amendments.md](.kiro/specs/windows-platform-enablement/cross-spec-amendments.md) 的两表保留原始 source 验收 SHA 与历史整理后当前可达的继承锚。`41f82b9` 最初已有 Current 表；`8e054db` 将原表命名 Historical 并增加当前锚表，`0dfe833` 收窄普通路线用途。旧表 8 项存在但不是当前祖先，新表 8 项均可达；这是 Git 寻址区别，未重跑或抹去原验收。标题宜更明确为“原始 source 验收锚”“当前继承的 source／构建前基线”；较早 `8fae` 应称“已验收候选”，不继续笼统叫“当前候选”。
- **文件链接**：`[文字](相对路径)` 是至少在 `41f82b9` 已使用的 Markdown 惯例；未找到强制每个文件引用都必须如此书写的仓库规则。对 README、steering 顶层、Windows spec 共 30 个 Markdown/115 个链接的检查未发现空目标、本机绝对路径或缺失本地文件；未验证全部远端 URL/标题锚。聊天的绝对可点击路径规则不应倒灌到 Git 文档。
- **旧过程噪声**：`frozen-build-bindings.md` 21–27、`frozen-build-scopes.md` 21–23 仍有 remediation、维护文件数量及重放目录等执行记录。以后收束时保留有效接口与 W3 适用范围，删除这类流水即可；不再造归档或新增治理文档。

这些文档事实修正可单独收束，不能被当作算法优化、产品政策或普通 frozen 发行的批准。本次临时评审分支保留原代码与历史。
