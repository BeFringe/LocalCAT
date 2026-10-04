# 中段评审补证：轮询异常、可比条件与来源核查

续接 `temp-fuzzy-performance-review@719ad1f` 的第 7 节 A/B/C。原运行代码为 `7b51cce583dbeb32d35047abbee614bbc609f457`。本文整理已有 CANDIDATE 原件，并在开发机执行一次严格报告 codec 核查；没有运行新的 Gate、产品或恢复，没有修改原报告或签发资格。DEV/VAL 分别表示开发机、验证机。QOS 和 source 插桩不混入本次两机对照。

共同候选为 `2addbf93a1287a11599ba4fcbb31456c909b13d01f5ea37eba118526baaf54c0`；实际报告 Core fingerprint 为 `1735533d07b1c5f185270a89c9cbb1e25699ed89ec2ed0460839b55ccd67f000`。两端 EXE SHA-256 均为 `fa21aa938118b09060ae769453ec92e5916b45b18f26bb25548cfecacb717743`。

## A. 轮询异常已确认到哪一层

[VAL-polling-traceback.txt](VAL-polling-traceback.txt) 是现场 stderr 的完整内容，保留原相对模块名、函数、行号和异常消息；Git 将换行统一为 LF，原件哈希仍指向本地原始字节。链路为：

`_poll_fuzzy_validation → tm_fuzzy_validation_status → _inspect_fuzzy_validation_for_controller → _fuzzy_validation_display → Gate D owner.status → condition.__enter__ → _OwnerWaitGuardLock.acquire → require_worker_wait_allowed`。

因此已确认与评审所述机制吻合：普通 packaged 的 UI 状态读取遇到 guarded condition 争用，抛出 `RuntimeError: ordinary worker wait cannot block the UI thread`。它不是 Gate D 的时间或 RSS 阈值失败。线程角色来自 Qt timer 连接和 guard 的源码判定；原日志没有独立记录线程 ID。

**边界尚不能再缩小为“只影响显示、没有其他影响”。** stderr 没有时间戳、epoch/generation、持锁线程或进入/退出 publication 的事件。不能确定异常发生在 oracle、migration、query、资格准备还是提交附近，也不能从这份栈判断 timer 后来是否完整展示了正确终态。异常可以中断该次 Qt 回调；不能据此声称 GUI 进程崩溃、数据损坏或整次 Gate 被取消。

同一 VAL 产品运行保存了如下事实；时间按原件显式记录，表内统一为 2026-10-03、UTC+08:00。**没有用文件 mtime/creation time 补造事件顺序。**

| 事件 | 原件所能证明的时间与结果 | 限度 |
| --- | --- | --- |
| 正常产品启动 | `06:01:20.5136853`；first-events 于 `06:01:23.995710` 成功 | 不代表 Gate 完成 |
| 启动恢复发现需重验 | startup log `06:01:25.462186`，`GATE_D.REVALIDATION_REQUIRED` | 位于手动验证前，不是本次最终性能失败 |
| 手动验证请求 | request 记录 `06:04:00.8847437`，一次正常 Settings Revalidate Fuzzy 点击 | 此记录是请求观察时间，不是 worker 开始/资格提交时间 |
| 实际 worker 观察 | migration-1 在 `06:13:16`；migration-2 在 `06:14:17–06:16:18`；query-1 在 `06:17:18–06:18:19`；query-2 在 `06:19:19–06:20:20` 被采样 | 每约 60 秒采样；缺少每个 child 的精确起止和退出码，不把“未观察到”当作退出事件 |
| 完整报告已存在 | `06:21:07.7708680` 的观察保存 report hash 与完整结果；两路各 100k、1200 Exact/240 Fuzzy 样本，distinct migration/query；两路 oracle 5000/200，`test_mode=false`、零漏项/漂移、recall=true | 没有精确的报告 rename/flush 完成事件；这是最迟已观察存在的时间 |
| 本机资格文件已存在 | `06:21:38.3733087` 的元数据观察；`06:22:43.5170312` 的内容观察记录 failed suite，artifact digest 与报告一致 | `IssuedAtUTC=2026-10-02T22:04:01Z` 是原字段，不能用作 commit 时刻；元数据提取明确没有校验签名 |
| 产品关闭 | `06:24:10.4278510`，记录为关闭设置后正常关闭；第一次因设置窗口使主窗 disabled 而未关闭 | `ExitCode=null`，不能补记为 0；没有本次 Gate 取消的时间记录 |
| 冷开观察 | `06:25:24.6031002`：Fuzzy 不可用/性能验证尚未通过；qualification bytes/hash 不变，未点击重验，未观察到 child/new Gate run | 证明观察到的产品状态；不等价于单独导出的 HMAC/compatibility 成功记录 |

轮询异常没有造成“完全没有结果可读”：这次 run 的完整失败报告与指向它的持久化资格元数据仍在，随后产品完成了关闭和冷开。现有材料不足以把异常精确排在上述某两个事件之间；也不足以判定当次 UI 全旅程仍通过。保留逐查询数据及其来源限度，生命周期项留待受控复现和修复后的真实候选闭环，不整批作废、不拼接其他 run 的成功片段。

## B. 两机可比条件

字段级原始摘录见 [original-facts.csv](original-facts.csv)；报告原件分别为：

- DEV：SHA-256 `9d2e91c68874a6d8f8c28436a1fb3f09bbdec9a1ecef2d6cd3c298f3689df0a5`。
- VAL：SHA-256 `87a4b394d343a0805745ac0f9f0a12bb91085bae223e5c517570bc3f8c730f90`。

| 条件 | 已有事实 | 仍然未知或不能推导的内容 |
| --- | --- | --- |
| 候选与实现 | 同一候选/EXE，实际 Core fingerprint 相同 | 相同候选不使设备环境相同 |
| 合同与路径 | `contract_json` 逐字相同，contract/suite/scorer/path digest 相同；两路 actual_index_kind 与 intended path 一致 | 不从路径名推断 SQL plan 相同 |
| 固定工作量 | corpus/fixture/composition、Exact/Fuzzy cohort、oracle subset 标识相同；各路100000记录，1200/240样本；实际 query_id 顺序均为 Exact 1–1200、Fuzzy 1–240 | 相同输入和记录数不是整个数据库所有关系的独立逻辑 dump 对比 |
| 测量口径 | 每 cohort warmup=100，measured_repeats=1，nearest-rank，perf_counter_ns，top-10，threshold=0.6；RSS 为 child-process-lifetime-v1 | 没有 OS cache 清理、冷热状态配对或随机化多轮顺序；只有一次正式运行 |
| 包内运行时 | CPython 3.14.7、SQLite 3.50.4、Unicode 16.0.0、Windows；路径对应 fts5_enabled=true/false | 原报告没有 SQLite compile_options、实际连接 PRAGMA 读回、query plan；版本相同不能代替这些观察 |
| 数据库 | 各自在本机迁移；两路 manifest_digest 相同；**两路 sidecar_digest 均不同** | 不能称 DB 逐字节相同；未采集物理布局/页状态/完整关系逻辑摘要及其成因 |
| 运行先后 | DEV 产品 `2026-10-03 05:05:19–05:22:12`，VAL 产品 `06:01:20–06:24:10`；各自后续冷开 | 不是同一时间窗口的同步对照；未记录 DEV 对应供电、系统负载、有效频率 |
| VAL 供电/系统观察 | Gate 期间17次采样全部 AC online、battery saver off；已观察 worker Normal priority，throttling masks=0/0；系统 `_Total` CPU 12–26% | 不能据此排除频率、热状态、内存、存储、其他进程或动态调度；原 ram_mib 为 `unknown` |
| 阶段成本 | VAL 有约60秒间隔的部分 worker CPU累计值；报告有完整 migration墙钟及逐query墙钟 | 无两机配对 SQL/JSON/验证/frontier/scorer CPU/墙钟/I/O；QOS/source 插桩不能补成此处的缺失事实 |

物理内容摘要来自 `query_facts.sidecar_digest`，由 benchmark artifact snapshot 对 SQLite sidecar 文件内容计算；不是 `artifact_baseline_digest`。后者还包含文件身份等信息，跨机器不同本身不能说明内容不同。

| 路径 | DEV SQLite sidecar SHA-256 | VAL SQLite sidecar SHA-256 |
| --- | --- | --- |
| FTS5_TRIGRAM | `3f7e7900bd3db384354be004e2331c1f32fc6d5acab2ab804796b038b7700a55` | `53f5b49b4509167fe6838cf7f63d4d137521a798c546d5df1067c271de5fc7b5` |
| GRAM_FALLBACK | `75d0071ecb61b65aa89c2b2ce29a07d0002cf4e7bb3252c9505c606d21db69be` | `1b607d77f3e0d33989dcdc68561edaa014875f53ba02525990ee2d79222da171` |

[VAL-runtime-samples.csv](VAL-runtime-samples.csv) 保留18次原观察的时间与数值，其中 sample 0 是验证前，1–17 在本次验证期间。只把临时 PID 映射为 migration-1/2、query-1/2，不附命令行、本机路径和其他进程。`none observed` 仅指这些 migration/query workers 当次未被采到，不能推导当时没有后台 oracle 或工作已经终止。两个 query worker 的已观察 CPU 差分分别为 69.734375s/60.483929s、69.921875s/61.214288s 墙钟；不是全寿命或 Phase 1 计时。`_Total` frequency 不是执行查询那个核心的有效频率。

**原报告结论不变：** DEV 两路 PASS；VAL FTS5 仅 `FUZZY_P95` 失败，fallback `FUZZY_P95`/`MIGRATION` 失败。VAL 的峰值 RSS 分别为415.296875/419.28125 MiB，在512 MiB之内。不能把此前其他候选出现的内存失败说成本次候选也失败；也不能由时间失败推导算错或漏查。

目前可确认输入/实现/测量合同一致，不能确认完整运行条件一致，也不能定位 CPU 或 SQL 为唯一根因。若继续原因实验，下一步只做两机固定8个既有查询加少量低成本/miss对照的工作量及阶段成本，并同步记录实际连接PRAGMA/query plan与运行条件；不改参数、不换资格、不重跑完整100k。若差异未集中在SQL，不扩展为物理DB交叉实验。

## C. 本地来源核查：现成结果与缺口

| 检查 | 已有原件 | 结论范围 |
| --- | --- | --- |
| 报告严格 codec | VAL received codec result 记录 `strict_codec_accepted=true`，report/bundle/fingerprint 与原件相符；既有 `inspect_sample_costs.py` 通过 `benchmark_evidence_bundle_from_json` 读取两份原报告后生成比较材料 | 这份接收端检查没有留存对应完整命令、stdout/stderr和退出码，不能补写“正式validator命令 exit=0”；codec验证不授予执行来源/本机资格 |
| report/attestation关联 | VAL metadata的artifact SHA/size与原报告相同，schema为 `localcat.gate-d-attestation.v2`，stored suite failed | metadata原件明确写签名未由此次提取校验，不能说共享材料验证了HMAC |
| DEV 冷恢复 | 相同资格hash，正常产品冷开后 `Fuzzy 可用`；formal与cold产品退出码均为0 | 正常产品观察支持恢复行为；没有独立逐检查的HMAC/compatibility结果日志 |
| VAL 冷恢复 | 失败资格hash不变，正常冷开后 `Fuzzy 不可用：Fuzzy 性能验证尚未通过`；未手动重跑 | 保留实际产品观察；不能把UI文案单独当作签名成功、Core结果或具体恢复分支的证明 |
| oracle/worker完整性 | 两报告都有双路径oracle/完整query facts，distinct process事实、完整样本和所需计数 | 缺少原机每个child stdout/stderr/exit-code的完整独立轨迹；不得由CSV重新签发receipt |

2026-10-04 在 DEV 实际调用正式 `benchmark_evidence_bundle_from_json` 读取 DEV 原报告及收到的 VAL 原报告副本，保留 stdout 和真实退出码于 [codec-check.txt](codec-check.txt)：两者均被严格 codec 接受，命令退出码为 0，报告 hash 与上文一致，suite verdict 分别为 True/False。调用时对 `tm_benchmark_gate.py`、`tm_contracts.py`、`tm_retrieval_capability.py`、`tm_benchmark.py` 执行 `git diff --exit-code 7b51cce -- <四个文件>`，退出码为 0；调用环境为 DEV 的 CPython 3.14.7。这是新增的接收端 schema/digest/verdict 核查，**不是在 VAL 上执行、也不包含 HMAC/恢复或新的 Gate 执行**。

这轮没有把接收端自洽性检查升级为原机资格校验。设备密钥和qualification原件继续留在原机；分享件没有秘密、签名或资格bundle，外援不能使用它授权产品。

下列是本次调用的可复现单文件形式（本次实际在同一进程对两个已列 SHA 的文件各调用一次；本机路径参数脱敏），在带原 `7b51cce` Core 的环境执行。`$env:ORIGINAL_REPORT` 指向待核查原文件；验证机无需为此安装 Python，不能把开发机检查冒称成验证机原机检查。

```powershell
python -c 'import hashlib,pathlib,os; from tm_benchmark_gate import benchmark_evidence_bundle_from_json; p=pathlib.Path(os.environ["ORIGINAL_REPORT"]); raw=p.read_bytes(); b=benchmark_evidence_bundle_from_json(raw.decode("utf-8")); print("report_sha256="+hashlib.sha256(raw).hexdigest()); print("bundle_digest="+b.bundle_digest); print("implementation_fingerprint="+b.implementation_fingerprint); print("suite_passed="+str(b.suite_report.passed))'
$LASTEXITCODE
```

应同时保留被调用源码SHA、原文件hash、stdout/stderr和真实退出码。异常拒绝原样记录类型/安全原因，不从“没异常”推断HMAC或当前设备兼容性已通过。

没有找到可直接宣称只读、又完整等价于生产Host恢复的现成 CLI。`_restore_gate_d_attestation_windows` 实际涉及Core会话、文件权限/绑定、私有证明和HMAC，不能临时用手写HMAC脚本替代它。补原机恢复细节应由现有 owner 正常冷启动/恢复接缝采集安全结果（先精确设计观察方式），只记录检查通过/拒绝、原错误码与状态，不导出密钥、不手工调用内部签发接口、不运行新Gate。产品启动本身会写启动日志，因此也不标成“纯只读命令”。

## D. 轮询修复与准入设计的分别收束

[`reassessment@b48ea2f`](https://github.com/BeFringe/LocalCAT/commit/b48ea2f) 已修复这条状态观察接缝：Host 按 owner condition → Host lock 非阻塞读取配对的已提交 lifecycle/能力展示，Controller 一次取得偏好和投影，Qt 两个入口在锁忙时保留展示并延期。prepare 后尚未提交的 SUCCEEDED 不可见；原严格查询 guard、publication 与性能政策不变。显式重验终态通过既有 queued bridge 刷新；投递时复核关闭状态，隐藏设置页不重启 timer。

实际 ordinary 锁竞争先复现 RuntimeError；修复后独立 reviewer 重放两个关闭序列及12项相关回归，退出码0；提交前 parent 对10项轮询回归复验，退出码0。另已执行原 Host 原子发布和 strict guard 相关回归。以上是 source/offscreen 范围；尚未构建或验收该修复的新 frozen 候选，不把旧运行的 UI 终态补记为 PASS。

本轮产品规则已明确为三个独立问题：启动恢复兼容资格、缺资格时保留手动验证；后台验证等待期间主体编辑/保存可用；完整验证后仅三项时间超限不单独拒绝 Fuzzy，报告仍失败，512 MiB 与正确性/召回/来源/完整性继续限制准入。实现当前仍采用旧准入，后续须在唯一 Core evaluator、发布一致性、恢复和安全警示投影中同步修改。现有 fingerprint 已覆盖策略模块，旧资格在策略变更后要求重验，无需新 profile。

日常查询异步另有具体未决接缝：Controller 同步查询锁之外，Host 在整个查询中仍持 lifecycle lock，现有 scorer budget 不是取消 token。不能只搬到 worker 就宣称编辑/关闭无等待。该设计与 Phase 1 成本优化各自保留有限范围，不反向扩大本轮准入规则或加入首次免验证。

具体合同草案位于 [`temp-fuzzy-admission-design@6d7f093`](https://github.com/BeFringe/LocalCAT/compare/b48ea2f...6d7f093)：ADR-029 由独立治理提交承载；Core、Feature5、平台分别修改现有 R/D/T 与审批元数据。旧完成项未改，准入代码尚未实施；日常异步仅安排最小接缝设计研究，没有把尚不存在的取消接口写成既有能力。

## 原件关联与脱敏

[original-sources.csv](original-sources.csv) 为每个逻辑来源列出原文件的SHA-256和字节数；CSV各行的 `source` 指向此表，不披露本机路径。原件仍保留在各自环境/本地收到的原始材料中，未被覆盖。`original-facts.csv` 只选择列明的原字段，除值的CSV转义/JSON紧凑表示外不改值；CPU型号字段整项排除，未把脱敏后环境伪称完整envelope。运行采样删除命令行、本机目录、主机/用户/其他进程，映射临时worker PID；traceback本来只有相对模块，文本逐行保留，分享件仅由Git规范换行。

原报告hash绑定的是未脱敏原件，**不是**让外援对删字段的摘录复算原报告digest或验证设备秘密。数字/记录可以复核本补充的论证，不能代替产品恢复bundle、真实执行receipt或新验收。本材料不加入新的产品证据schema、ledger或治理要求。
