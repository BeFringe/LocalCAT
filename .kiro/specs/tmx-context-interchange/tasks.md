# 实施任务

> **WA-05 Windows compatibility amendment**：以下 `a` 后缀任务只接入 Parser sealed source 与 ADR-020 publisher；TMX grammar、scope、loss、ResourcePackage profile 与 receipt authority 不变。

## Cluster 0：合同与安全基线

- [x] 1.1 冻结 scope × profile × carrier capability matrix、effective locale、inclusion/loss 与 prop registry。
- [x] 1.2 建立 frozen TMX contracts、limits/error codes 与架构 guard。
- [x] 1.3 扩展 Parser TMX reader 保留 ordered/duplicate props，保持 hostile XML/limits 回归。

## Cluster 1：Semantic Import 与 Deterministic Writer

- [x] 2.1 实现 canonical record ↔ TMX unit 映射、LocalCAT props 与 unknown prop round-trip。
- [x] 2.2 实现 deterministic Level 1 writer、loss report 与 Parser cold validation。
- [x] 2.3 接入 TMX import draft，验证 context/provenance/status、缺 context、unknown props 与 inline XML 失败语义。
- [x] 2.3a 在 Windows 仅消费 Parser-issued sealed TMX source；reparse/hardlink/root drift 与 rooted capability failure 在 draft/Store mutation 前 fail closed。
  - _Amendment: WA-05_
  - _Depends: 2.3, WA-01, windows-platform-enablement 3.7_
- [x] 2.4 完成真实 MateCat/LocalCAT fixture、hostile/fault、round-trip 和 compatibility tests。

## Cluster 2：Resource / Workspace / Chunk Direct Export

- [x] 3.1 建立 managed TM complete snapshot adapter 与 preview/apply stale revalidation。
- [x] 3.2 建立 Workspace session+universe exact join 和 entire-project export。
- [x] 3.3 叠加一个明选 Chunk scope projection，处理 detached/missing/foreign/stale。
- [x] 3.4 实现 destination binding、candidate/LKG、cold validate、atomic publish、receipt/recovery。
- [x] 3.4a 将 TMX destination publication 接入 `BoundDirectoryPublisher` 与 `ProcessFileLock`，保持 candidate/LKG/cold validate/receipt 顺序。
  - _Amendment: WA-05_
  - _Depends: 3.4, ADR-020, WA-01, windows-platform-enablement 2.1_
- [x] 3.4b 集成 direct TMX 成功终结与 Windows 输出锁收尾
  - 在本次 retained readback、receipt-ready、terminal 与 owned sidecar/journal 收尾全部完成后消费可选平台能力；稳定 journal 仍存在或原 recovery-required 时保留锁，不改变原恢复状态机。
  - 完成时，resource/project/chunk 三种 direct 导出在无竞争时只保留最终 TMX，Parser 冷重开、scope/loss 和原 receipt 一致；收尾占用或不确定不推翻已闭合结果。
  - 覆盖正常冷恢复、无 journal 返回、未闭合 journal/sidecar、原发布/恢复异常、平台收尾失败和真实 holder/waiter 竞争；ResourcePackage 仍由 LRP 收尾，POSIX 与其他锁不变。
  - _Requirements: 8.5, 8.7, 8.8, 8.9, 11.1_
  - _Boundary: TMX Direct Artifact Saver 与平台输出锁终结集成_
  - _Depends: 3.4a, windows-platform-enablement 3.3a, ADR-027_
- [x] 3.5 完成 resource/project/chunk exact scope、inclusion/loss 与 publication fault matrix。
- [x] 3.5a 在 Windows 覆盖 publish phase crash、锁竞争、destination replacement 与 clean-process recovery，失败不改 scope owner 或 source artifact。
  - 每个失败必须证明target-before exact不变；rename后事实不确定时只进入recovery-required并保留LKG/journal，不得报告成功或猜测清理。
  - _Amendment: WA-05_
  - _Depends: 3.4a, 3.5, windows-platform-enablement 3.7_

## Cluster 3：ResourcePackage TMX Profile

- [x] 4.1 为 LRP 增加新的 exact schema/carrier/profile-set triple 与 TMX payload handler protocol。
- [x] 4.2 仅为 managed resource 封装 deterministic TMX payload；project/chunk capability 负向拒绝。
- [x] 4.3 接入 package cold validate/publication/receipt/recovery，复用 LRP manifest/ZIP/transaction owner；TMX package import/apply 负向拒绝。
- [x] 4.4 完成 package tamper、profile mismatch、apply fault 与 JSONL/CSV v1 exact compatibility。

## Cluster 4：Controller、Qt 与收尾

- [x] 5.1 接入 Controller typed resource/project/chunk preview/export commands。
- [x] 5.2 项目菜单增加“导出项目”，资源页增加“导出 TMX”；完成非阻塞 preview/export UI。
- [x] 5.3 用真实 canonical TM、多文档项目和 active chunk 完成三 scope journey；用 TMX ResourcePackage 完成冷重开事务。
- [x] 5.3a 在 Windows 源码运行中重放 TMX import、三 scope export、ResourcePackage 冷重开和 hostile XML/source journey。
  - _Amendment: WA-05_
  - _Depends: 2.3a, 3.5a, 5.3, WA-03, WA-04, WA-06_
- [x] 5.4 运行 Parser/TM/ResourcePackage/Project/Chunk/Qt/fault/architecture/full regression，更新 current-source steering/evidence。
- [x] 5.4a 在普通 frozen 候选完成 TMX 真实旅程
  - 从实际 EXE、non-repository CWD 执行 direct import、三 scope export、冷验证/重开和 package export-only 负向边界；必要数据来自普通 bundle，无 checkout 回退，不要求 Parser `.py` 源码证明。
  - 候选执行 rooted source 拒绝、零目标 mutation 与恢复反例，按平台 Requirement 12 复用未变 owner 深入证据并重验触发范围；退出时向 Qt 交付真实候选 owner port 和结果，不以 source 验收代答。
  - _Amendment: WA-05_
  - _Delivery phase: frozen post-build_
  - _Depends: 5.3a, ADR-028, windows-platform-enablement 7.2, windows-platform-enablement 3.7_
- [x] 5.5 对照 Requirements/Design 验收，无 silent scope cut 后提交 `feat(tmx): 建立上下文互操作导出`。

## 明确禁止

- 用 current row/document/search 或显示顺序替代 exact project/chunk scope。
- 将 project/chunk 先转成 managed resource，或包装成 ResourcePackage。
- 在 ResourcePackage 模块实现 XML grammar，或在 TMX 模块实现 package manifest/apply/receipt。
- 静默丢弃 unknown prop、detached、empty target 或 blocking loss。
- 用“TMX 可被 XML parser 打开”代替 Parser 业务 reader cold reopen。
