# 研究与设计决策

## 已有事实

- `parser_tmx_codec.py` 已有安全流式 reader/validator，但 `canonical_write=False`，没有 writer；当前 `ResourceRecord.format_metadata=()`，因此 `<prop>` 被丢弃。
- `SQLiteTMStore.capture_export_snapshot()` 可在一个 read transaction 中给出完整、有序、绑定 revision 的 canonical TM snapshot。
- `WorkspaceSessionView` 提供正文、locale 和项目顺序；`ProjectWorkspaceService.capture_workspace_universe()` 提供 project/session/revision/composition/digest 与 attached/detached presence。
- `ChunkScopeProjection` 提供单个 active chunk 的 plan/revision/digest/universe/chunk exact membership，不提供正文或顺序。
- ResourcePackage v1 只批准 JSONL/CSV；若加入 TMX 必须新建 exact schema/carrier/profile-set triple，container/apply/receipt authority 不变。

## 冻结决策

1. **三轴分离**：source scope、payload profile、carrier 分别建模。项目/分工 direct-only；managed resource 才可 direct 或 ResourcePackage。
2. **一个 selected chunk**：export 不自动合并多 chunks，也不从 current row/search/doc 猜范围。
3. **Workspace join**：项目 = session view + universe projection；分工 = 前两者 + chunk projection，全部按 stable identity join。
4. **Inclusion**：empty target 排除；unconfirmed 和 source=target 包含但警告；detached 排除并显式计数；missing/foreign/stale/不可表示 metadata 阻断。
5. **Locale**：`und` 不进入 writer 猜测。UI 灰字建议 `en` / `zh-CN`，preview 将用户确认的 effective locales 固化进 plan。
6. **Prop policy**：保留未知 prop 的 type/lang/value/order/duplicates；仅映射有证据的 registry 项。首轮识别 `x-MateCAT-status` 为 status/provenance，不猜 vendor context。
7. **Context export**：LocalCAT canonical context 使用 profile-owned `x-LocalCAT-context-prev` / `x-LocalCAT-context-next`；speaker、file source、confirmed/status 和 provenance 使用稳定 `x-LocalCAT-*` props。Imported unknown props 仍原样输出。
8. **Publication**：direct TMX 有自己的 scope+destination plan/receipt，可复用 carrier-neutral dirfd/fsync/digest 原语，不复用 ProjectPackage/ResourcePackage 语义 DTO。
9. **ResourcePackage successor**：LRP 注册 TMX payload handler；TMX 不 import/创建 manifest、ZIP、package apply plan 或 resource receipt。

## 未采纳方案

| 方案 | 原因 |
|---|---|
| 将 project/chunk 先升级为 managed resource 再导出 | 改变 scope identity，制造不必要的 Resource authority |
| 项目/分工都封装 ResourcePackage | ResourcePackage 语义是单 managed resource 完整快照，不是 workspace/job carrier |
| 从显示顺序或文本恢复 chunk membership | rename/reorder/reconciliation 后会漂移 |
| 未知 prop 全部映射为 context | vendor 语义不稳定，会错误影响 context 排序 |
| 空 target 也写 TU | 生成不可用翻译单元且污染下游资源 |
| 用宽松 XML library 另写验证器 | 回退 Parser 的 DTD/ENTITY/limit 安全基线 |
