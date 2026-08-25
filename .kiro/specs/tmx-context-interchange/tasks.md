# 实施任务

## Cluster 0：合同与安全基线

- [x] 1.1 冻结 scope × profile × carrier capability matrix、effective locale、inclusion/loss 与 prop registry。
- [x] 1.2 建立 frozen TMX contracts、limits/error codes 与架构 guard。
- [x] 1.3 扩展 Parser TMX reader 保留 ordered/duplicate props，保持 hostile XML/limits 回归。

## Cluster 1：Semantic Import 与 Deterministic Writer

- [x] 2.1 实现 canonical record ↔ TMX unit 映射、LocalCAT props 与 unknown prop round-trip。
- [x] 2.2 实现 deterministic Level 1 writer、loss report 与 Parser cold validation。
- [x] 2.3 接入 TMX import draft，验证 context/provenance/status、缺 context、unknown props 与 inline XML 失败语义。
- [x] 2.4 完成真实 MateCat/LocalCAT fixture、hostile/fault、round-trip 和 compatibility tests。

## Cluster 2：Resource / Workspace / Chunk Direct Export

- [x] 3.1 建立 managed TM complete snapshot adapter 与 preview/apply stale revalidation。
- [x] 3.2 建立 Workspace session+universe exact join 和 entire-project export。
- [x] 3.3 叠加一个明选 Chunk scope projection，处理 detached/missing/foreign/stale。
- [x] 3.4 实现 destination binding、candidate/LKG、cold validate、atomic publish、receipt/recovery。
- [x] 3.5 完成 resource/project/chunk exact scope、inclusion/loss 与 publication fault matrix。

## Cluster 3：ResourcePackage TMX Profile

- [x] 4.1 为 LRP 增加新的 exact schema/carrier/profile-set triple 与 TMX payload handler protocol。
- [x] 4.2 仅为 managed resource 封装 deterministic TMX payload；project/chunk capability 负向拒绝。
- [x] 4.3 接入 package cold validate/publication/receipt/recovery，复用 LRP manifest/ZIP/transaction owner；TMX package import/apply 负向拒绝。
- [x] 4.4 完成 package tamper、profile mismatch、apply fault 与 JSONL/CSV v1 exact compatibility。

## Cluster 4：Controller、Qt 与收尾

- [x] 5.1 接入 Controller typed resource/project/chunk preview/export commands。
- [x] 5.2 项目菜单增加“导出项目”，资源页增加“导出 TMX”；完成非阻塞 preview/export UI。
- [x] 5.3 用真实 canonical TM、多文档项目和 active chunk 完成三 scope journey；用 TMX ResourcePackage 完成冷重开事务。
- [x] 5.4 运行 Parser/TM/ResourcePackage/Project/Chunk/Qt/fault/architecture/full regression，更新 current-source steering/evidence。
- [x] 5.5 对照 Requirements/Design 验收，无 silent scope cut 后提交 `feat(tmx): 建立上下文互操作导出`。

## 明确禁止

- 用 current row/document/search 或显示顺序替代 exact project/chunk scope。
- 将 project/chunk 先转成 managed resource，或包装成 ResourcePackage。
- 在 ResourcePackage 模块实现 XML grammar，或在 TMX 模块实现 package manifest/apply/receipt。
- 静默丢弃 unknown prop、detached、empty target 或 blocking loss。
- 用“TMX 可被 XML parser 打开”代替 Parser 业务 reader cold reopen。
