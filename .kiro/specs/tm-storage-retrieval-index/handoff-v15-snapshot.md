# Feature 5 v15 临时换机接任快照

> 临时性：本文件只用于跨电脑恢复任务上下文，可能在原始快照提交后继续追加修正。新环境完成接管、且本文件不再承担恢复权威后，应以单独提交删除本文件；不要逐个 revert handoff 更新，也不要回退其中承载的治理或实现 checkpoint。
>
> 日期：2026-08-14

## 1. 身份与边界

- 源机器唯一实施 worktree：`/home/neotag/文档/CAT/localcat-feature5`。该绝对路径只是旧环境身份；新机器必须由用户重新明确授权唯一 worktree，再从其 Git root 解析路径，不能因本文件出现旧路径就改在其他 checkout 实施。
- Git branch：`feature5`
- 原始临时归档提交：`5cda94b52200dfd77eef79f05236c8f3d54b9731`
- 本快照的父提交：`12fb5a5c2ef5376109d27b9592c696766e2d2c82`（v14 治理）
- 本文件可能有后续 doc-only 修正；用 `git log --follow -- .kiro/specs/tm-storage-retrieval-index/handoff-v15-snapshot.md` 重建，而不是假定原始归档提交就是最新提示词。
- 当前 Requirements 不变；Design、Tasks 与评审集群已在本快照中推进到采纳稿 v15。
- Task 8.6、8.7、8.9、9.5、9.6 均保持未勾选。
- Cluster M 第二轮仍待实施闭合与原生 xhigh 对抗复审；Cluster N 保持已闭合；Cluster O 在 M 重新通过前阻塞。
- 原始归档提交故意保存未闭合 tracked 混合态，供换机恢复。它不是 Task 完成记录，也不能单独授予 correctness、performance 或 release authority。

### 设计约束清单

- **设计要求：** scorer-v1、2048 invocation budget、100k frozen corpus、`.60` threshold、真实 top-10、500 ms fuzzy p95、120 s migration 与 512 MiB RSS 均不得放宽；FTS5/fallback 必须分别通过。
- **已实现：** v13 scorer-call/identity 双域与 Retrieval-owned exact-fold reuse；v14 的 U1/K0/R/U2 两阶段骨架、跨 phase binding 与 A0/P1/R/A1/P2 contract；Cluster N sealed/active attestation 已闭合。
- **待实现：** v15 exact-LCS U3、ordered folded projection、全部 40 miss 的 budget 闭合、20-sample production-shaped cheap gate、Cluster M 第二轮 review、Cluster O fresh Gate D/evidence/release。
- **未授权：** 提高 budget、挑选有利 miss、修改 corpus/threshold/top-k/gate、以 component heuristic 或偷跑 Levenshtein/scorer 代替上界、增加持久 schema/index、提前恢复 Cluster O。
- **Critical Path：** 换机身份重建 → Task 8.6/8.7 v15 → Cluster M xhigh review → Task 8.9 fresh Gate D → 9.5/9.6 → Cluster O final review → Qt handoff。
- **隐性依赖：** Cluster O 的 source roots、oracle/benchmark bundle、86 条映射与 release decision 都依赖 Cluster M 最终代码和新鲜性能事实；M 未闭合时，即使局部测试全绿也不能刷新或发布 O。

## 2. 为什么从 aa4916b 走到 v15

`aa4916b` 记录了 Task 8.4/8.5 的诚实 Gate D NO-GO：旧 overlap/truncation 召回只达到 0.94，FTS5 与 fallback 各遗漏 27 个真实 top-10 identity；fuzzy p95 与 migration 也超过冻结的 500 ms/120 s。它新增 M/N/O 补救审批簇，不放宽 scorer、budget、corpus、threshold、top-k 或硬门。

后续真实 100k 路径依次推翻了三个实现假设：

1. v13：budget 限制真实 scorer-v1 调用，不是 record identity；重复源 query 61 有 3000 identities、300 exact-fold classes，按 identity 计数会错误耗尽 2048 budget。逐 block 事务还会打开 94% 以上 block。
2. v14：一次性全量 exact frontier 正确但 q61 warm latency 约 563–916 ms；两阶段 `U1→K0→R→U2` 把 q61 降至约 355–362 ms，但字符/bigram multiset 丢失顺序。
3. frozen q240 的真实 kth 只有 `(0.11472995090016369, 20040)`；即使使用理想 U2 顺序，仍有 3731 个互异 fold 类可能进入 top-10，数学上必然超过 2048 budget。这不是 batch、selector 或实现推进缺陷。

v15 采用当前 schema 上的 exact-LCS 顺序下界。令 folded code-point LCS 长度为 `ell`，`L=max(m,n)`，精确 bigram 差异为 `G2=Bq+Br-2I`：

```text
d3 = max(abs(m-n), L-ell, ceil(G2/4))
U3 = (1-d3/L + exact_bigram_dice) / 2
真实 scorer-v1 分数 <= U3 <= U2 <= U1
```

LCS 只生成 Levenshtein 距离下界，不得计算编辑距离/final score，也不得授予 scorer 等价类。Retrieval 仍必须从 health-validated raw record 重建完整 fold-v1 等价类并独占真实 scorer-v1 调用。

只读原型在不新增 schema/index 的情况下，把 40 条 frozen miss 的最坏 U2 竞争类从 13111 降为 U3 的 473；q240 从 3731 降为 302。q226/q240 的非完整 production prototype warm p95 约 357/342 ms，只是可行性证据，不能替代正式 cheap gate 或 Gate D。

## 3. 本快照保存的 tracked 混合态

### 3.1 Cluster M 未闭合代码

下列文件包含 v13 已验证语义、v14 两阶段实现和少量诊断 selector；它们尚未按 v15 收敛：

- `tm_contracts.py`：`proof-query-v2` 双域计数与 `proof-traversal-v2` nested refinement metadata；冻结 A0/P1/R/A1/P2、K0、请求/返回与前沿。当前仍以 U2 语义命名/验证，需改为 ordered LCS/U3。
- `tm_sqlite_store.py`：phase 1 length+exact bigram 与 phase 2 ordered-R exact character facts；两个短 read transaction 均有 generation/head/count/query/maxima binding。v15 应把 phase 2 改成 private ordered `record_id/source_fold_v1/length` 投影，并在事务提交后计算 exact LCS。
- `tm_candidate_index.py`：sparse 与 two-phase dense session、U1/U2、K0/R、双 frontier 与 budget accounting。当前另有诊断性的 flat coarse-max dense crossover：`>=8 blocks` 且 threshold 或 `0.75*max` 条件覆盖 75% block。selector 只改变遍历、未排除 identity，因此语义安全，但该具体常量未批准；v15 必须把 selector 冻结为纯性能选择并以测试证明不授予 completeness。
- `tm_retrieval.py`：Retrieval-owned full-fold equivalence reuse；一类只运行一次 concrete scorer-v1，raw source/target/provenance/id/tie 独立保留；custom/injected scorer 无证明权威。该所有权必须保留。
- `tests/test_tm_candidate_proof_query.py`：v2 codec、双域守恒、U1/U2、等价类、dense parity、race、budget、partial batch 与诊断 selector 覆盖。需加入 U3/LCS、40 miss、ordered projection 和三段 race 的正式负例。

当前混合态不得标记 Task 8.6/8.7 完成。最近机械证据：focused proof-query 32 tests 通过；此前 v14 owner suite 295 tests 通过；changed-file basedpyright 0 errors；`git diff --check` 通过。20-sample completion gate 因 q240 U2 数学阻塞而未执行/未宣称。

### 3.2 Cluster O 提前形成但未完成的 tracked 代码

- `tm_benchmark_gate.py`
- `tests/test_tm_benchmark_gate.py`

这两处是 Task 8.9 首轮的 oracle-first 顺序修正：先跑固定 5k 双路径 oracle，任一路遗漏都不得启动 100k migration/query；包含 3 个顺序/失败单元测试。快照前 SHA-256 分别为：

```text
c739c684fa58f1270d1c5e679dd46ef319b77b7976bcf0aa87ccda049289b32a  tm_benchmark_gate.py
385a263f40a3c4d74a3e93e8d9bce9aaf49c8297ea498a942c74363c915cecdc  tests/test_tm_benchmark_gate.py
```

此前 focused Gate D 为 64/64、basedpyright 0；但唯一真实 owner run 在旧 q61 proof budget 处失败，没有生成新 bundle，旧 `benchmark_tm_evidence.json` 未修改。该代码随临时快照保存，不表示 Task 8.9 完成；正式 Cluster M 通过前不得继续 O。

## 4. 关键验证与反例

### 4.1 已闭合/可复用事实

- q1：SPARSE，约 69 ms，2048 inspected、666 accounted、5 calls。
- q28 short：SPARSE，约 11 ms，256 inspected、10 accounted、1 call。
- q61：v14 DENSE，约 355–362 ms；3000 accounted、300 exact-fold scorer calls、R=3030；top-10 为 record 5070→5061。
- exact-fold reuse 只能由 Retrieval 对完整 fold-v1 equality 建立；hash、length、gram、seed、caller group 或 injected scorer 均不能建立等价。
- scorer budget 固定为 2048 invocation；identity fan-out 不增加调用数。

### 4.2 q240 阻塞证明

- corpus：100000 identities、92327 exact-fold classes；raw→fold validation 100000/100000。
- true threshold `.60` identities/classes：0/0。
- true top-10 ids：`50040,40046,94009,64049,40900,40498,24009,40091,40421,20040`。
- true kth：`(0.11472995090016369, 20040)`。
- U1 在 true kth 仍竞争：99000 identities / 92323 classes。
- U2 在 true kth 仍竞争：3731 identities/classes；理想 best-first 也至少需 3731 calls，因此 v14 必然失败。
- schema-tagged oracle identity digest：`e607af6ba7f4c1042c35a0c97d7d562bbbe8b04e042d1cacbd7409379d45c81e`。
- digest 算法：对 `{schema:'proof-query-v2-oracle-identity-v1', ordered threshold_record_ids, ordered top10_record_ids}` 做 sort-keys、无空白 canonical JSON UTF-8 SHA-256。

q201 对照：U2 最少约 1069 calls，可闭合但 production 约 730 ms；oracle identity digest 为 `5af2adb23f9bd0fbb255315eff9a498cd48298fde5e8f1b3a57cd6a143a0badc`。

## 5. 未入 Git 的 disposable 100k artifact

目录 `.feature5-task-8-9-diagnostic-fts/` 约 374 MiB，仅为本机可重建诊断资产，故意不纳入提交/推送。快照时根目录 device/inode 为 `51/22785665`。五个文件身份：

```text
7f208ae2daef26d3f7f913444094e7e5319f444b66e5fb3f848ca5469db1d10b  fixture.jsonl                         size=12970212  inode=22785666
af455fbaf0271e7d6e97b8a2e333e2322788af933796ec2855a32cc25ed12d5c  fixture.jsonl.sqlite3                 size=374321152 inode=22785667
32b221003b6b3b7abb4762c458ca59a38acdc37eb869e08ec3ec979094510a32  fixture.jsonl.localcat-snapshot.json  size=533      inode=22785735
efb8ca6aa675c7154631eb30fb0dc8649fc606efd78a68cf245650c4d70c51ce  .fixture.jsonl.sqlite3.localcat-activation-journal.json size=6978 inode=22785947
8f0fa298e3332d0443f8ac8521c6450277a4b9a2de3dabbd40c000fa712d8862  .fixture.jsonl.sqlite3.localcat-activated-lineage.json size=239 inode=22785950
```

换机后不应从未知来源复制或信任该目录；应由 frozen benchmark corpus/migration owner 新鲜重建，并重新绑定新设备 inode/full SHA-256。现机器上在不再需要前也不得按模糊路径删除。

## 6. 主 agent 后续顺序

1. Clone/fetch `git@github.com:BeFringe/LocalCAT.git`，由用户明确授权新机器唯一 `feature5` worktree；从该 Git root 完整读取 `AGENTS.md`、全部 `.kiro/steering/`、spec.json、requirements.md、design.md、tasks.md 与本文件，核对 root/branch/full HEAD/status，不假定旧绝对路径仍存在。
2. dispatch 同一职责的原生 xhigh implementation worker，收敛 v15 current-schema LCS；不得让 worker stage/commit。
3. 主 agent 从磁盘审查 diff，运行 cheap gate、owner suite、Cluster N tamper regression、basedpyright 与 diff check。
4. 只有 Task 8.6/8.7 completion definition 全部满足时才同步勾选、写一条自包含 Implementation Note，并形成正式小步提交。
5. 对 Cluster M 的累计 base..tip 做一次原生 xhigh code-reviewer 对抗复审；修正后跑一次 fresh Cluster M suite。
6. M 闭合后才恢复 Task 8.9：运行一次新的 5k oracle + 100k FTS/fallback owner pipeline，生成新 evidence bundle；不得复用旧失败 run。
7. 完成 9.5 evidence roots/86 条映射刷新与 9.6 full suite；Cluster O 做一次原生 xhigh 累计复审。全部 Feature 5 tasks 完成后才向 Qt 增量任务交付。
8. 新环境完成接管且本文件不再承担恢复权威后，按第 9 节用新的非破坏性提交删除临时文档。不得直接 revert 原始归档提交；若用户还要求从永久历史移除 WIP checkpoint，必须另做有备份、有树等价验证且单独授权的历史整理。

## 7. 原生 xhigh worker 接任提示词

以下文本可直接作为新会话的独立 implementation assignment；调用方应补入恢复后实际的完整 HEAD OID，不得只给短哈希。

```text
你负责 Feature 5 Cluster M v15 的 Task 8.6+8.7 current-schema LCS 收敛实现。你不是代码库中的唯一 agent；保留其他人修改，不得回滚、覆盖或顺手清理不属于你的内容。

授权工作根：<新机器上由用户明确指定的唯一 feature5 worktree 绝对路径>
branch：feature5
exact base：<恢复后 git rev-parse HEAD 的完整 OID>
源机器旧路径 `/home/neotag/文档/CAT/localcat-feature5` 只用于核对历史身份，不能自动成为新机器权限。先 fail-closed 核对授权 root/branch/full HEAD/status；不匹配立即停止。不得创建/switch worktree或branch，不得访问替代 checkout，不得 stage/commit/push。

开始前完整读取：AGENTS.md；全部 .kiro/steering/（重点 feature5-review-clustering.md v15）；.kiro/specs/tm-storage-retrieval-index/spec.json、requirements.md、design.md、tasks.md、handoff-v15-snapshot.md。Requirements、Design v15、Tasks 与 steering 是权威；snapshot commit 是未闭合恢复态，不是正确性授权。

你拥有的 tracked paths，仅按需要修改：
- tm_contracts.py
- tm_sqlite_store.py
- tm_candidate_index.py
- tm_retrieval.py
- tests/test_tm_contracts.py
- tests/test_tm_sqlite_store.py
- tests/test_tm_candidate_index.py
- tests/test_tm_candidate_proof_index.py
- tests/test_tm_candidate_proof_query.py
- tests/test_tm_retrieval.py
- tasks.md

明确排除：tm_benchmark_gate.py、tests/test_tm_benchmark_gate.py 及任何 benchmark evidence；它们是 Cluster O 的独立未闭合快照。不得修改/删除本机 .feature5-task-8-9-diagnostic-fts；若换机不存在，只能由 owner 新鲜重建，不得伪造 inode/digest。

实现目标：
1. 保留 proof-query-v2 的 scorer invocation/accounted/unscored identity 双域守恒、Retrieval-owned exact full-fold reuse、sparse path、threshold+top-k 双闭合、budget=2048 和资源级 fail-closed。
2. 保留 phase 1：同一 generation-bound 短 read transaction 获取 length + exact bigram I，形成安全 U1；commit 后按 U1 建立真实 K0，由 session 独占派生 R。
3. 把当前 phase 2 exact-character/U2 改为 proof-only private ordered projection：在重新绑定 resource/store/generation/head/count/query/maxima 后，只为严格有序 R 读取 record_id/source_fold_v1/length，拒绝 missing/duplicate/reordered/out-of-range/extra；事务提交后对 folded Unicode code-point 序列计算 exact LCS ell，形成 d3=max(abs(m-n),L-ell,ceil(G2/4)) 与 U3。
4. 证明 true scorer-v1 <= U3 <= U2 <= U1。single-character Dice 沿用 scorer-v1。LCS 不得计算 Levenshtein/final score，不得授予等价类；store 不得返回 scorer evidence；Retrieval 仍从 health-validated raw record 重 fold 建类。
5. proof contract 冻结 ordered-bound/traversal version、A0/P1/R/A1/P2、K0、request=returned、P1 max U1 与 P2 max U3 frontier；total=A0+P1+R、R=A1+P2、accounted=A0+A1、unscored=P1+P2、invocation=accounted exact-fold classes<=budget。tie equality未闭合。public payload不得含正文、fold、LCS、gram或等价键。
6. 两个 read transaction 都不得跨 scorer；append before/during/after phase 2及final validation前均稳定 STORE.CANDIDATE_PROOF_STALE。operation lease保持generation替换隔离。
7. flat/dense selector只能是确定性的性能选择，不得授权completeness。不要提高budget、改corpus/threshold/top-k、挑有利miss、加入persistent schema/index、偷跑scorer/Levenshtein或使用component heuristic。

必需测试：
- 穷举小字母表和固定 Unicode 随机向量：score<=U3<=U2<=U1；single-char；aaaaa/aaaba；tie equality。
- LCS/ordered projection少报、超范围、错序、缺失、重复、额外id、query/head/maxima/binding伪造全部fail-closed。
- phase 2前/中/后与scorer期间append race。
- caller/hash/gram/length/injected scorer不能伪造fold equivalence；budget只计真实scorer调用。
- q61 3000 identities/300 calls；全部40 frozen miss均<2048 calls；q240 oracle top10与handoff文档一致。
- q1、q61、short、q226、q240各至少20个warm production-shaped样本，nearest-rank internal p95<=400ms；包含binding、ordered read、LCS、record fetch、真实scorer、finish/final generation validation与RSS。FTS/fallback共用同一proof state machine。

机械验证：相关 owner unittest suite；Cluster N tamper/attestation regression；changed-file basedpyright --level error；git diff --check。不要跑完整Gate D。全部通过后才把Task8.6/8.7勾选并更新一条简洁、自包含的Implementation Note；否则保持unchecked，报告最小真实阻塞。返回精确root/branch/full HEAD、changed paths、测试命令/计数、性能样本定义/结果、未解决项与final status；仍不得stage/commit。
```

## 8. 推送与相邻 worktree

- 三个远端 head 已通过 SSH 推送并核对：`feature5@5cda94b52200dfd77eef79f05236c8f3d54b9731`、`ui-mvp@fd1b732ced807f344dff420b887834946fe9a6a7`、`governance/kiro-steering@4905a3f51e2b70a37cf4eb72ec3cf620e1e714f0`。本文件的后续修正以 `feature5` 更新为准。
- 源机器 `ui-mvp` worktree 是 `/home/neotag/文档/CAT/CAT`，存在用户未提交文件；这些文件未进入上述远端 head，新机器不得从“已推送”推断它们已备份。
- 源机器 `governance/kiro-steering` worktree 是 `/home/neotag/.local/share/opencode/worktree/bd10770111131a050c457174553a67d555e13df2/jolly-orchid`，存在未跟踪 `.opencode/bun.lock`；目录名只是托管 worktree 名，该文件同样未备份。
- 后续 push 使用 SSH 且不得 force push；除非用户在完成正式 checkpoint 后另行批准第 9 节所述历史整理。

## 9. 临时快照撤回方式

默认“撤回”只表示移除已经失去恢复用途的 handoff 文档。新机器完成身份重建、读回本文件、确认正式 v15 checkpoint 与后续计划已有其他持久权威后，使用一个新的非破坏性清理提交：

```bash
git rm .kiro/specs/tm-storage-retrieval-index/handoff-v15-snapshot.md
git diff --cached --name-status
git commit -m "docs(handoff): 移除 Feature 5 v15 临时快照"
git push origin feature5
```

不要直接 `git revert 5cda94b`：该提交同时承载 v15 前混合代码、Cluster O 提前代码和 v15 治理，机械 revert 会反向修改正式实现的祖先内容。也不要使用 `git reset --hard`、无备份 rebase 或普通 `--force`。

如果用户在 Task 8.6/8.7、Cluster M、Cluster O 都已有正式 checkpoint 后，仍明确要求从永久历史中删除 WIP 归档提交，则把它视为独立的历史重写任务，而不是文档清理：先保存可恢复 ref/bundle 与远端 OID，冻结预期最终 tree，重写后重新运行对应 fresh tests/证据并逐文件比较 tree，只允许 `push --force-with-lease`。该动作需要届时的单独授权，不能由本 handoff 文档预先授权。
