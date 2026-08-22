# Brief: tmx-context-interchange

## Problem

MateCat/MyMemory 会通过 TMX vendor properties 或 API 参数携带 provenance/context，但不同导出样本并不一致。把未知 `<prop>` 猜成上下文会产生错误排序，完全丢弃又会损失可追溯信息。

## Current State

LocalCAT 已安全导入 TMX Level 1 文本和部分 MateCat 样本，但 canonical TM record、未知 prop 保留、context 映射与 TMX export 尚未形成独立契约。

## Desired Outcome

用户导入受支持的 TMX 时可保留 provenance，并仅在属性名/语义经样本验证后映射上下文；导出范围清楚，不把缺少 context 的文件误报为损坏。

若 TMX export profile 后续获批，它可作为 `language-resource-portability` 的一个可选 ResourcePackage profile 被调用；本规格拥有 TMX payload grammar、context/provenance 映射与有损报告，不拥有 ResourcePackage container、资源 import/apply transaction 或 provider 传输。

## Approach

以 profile 驱动的 TMX 互操作层处理标准字段和经验证 vendor props；未知属性作为原始 metadata 保存。该规格消费 canonical TM store，而不是在 Parser 内实现检索。

## Scope

- **In**: 标准 TMX 文本、语言、TU/TUV 标识、已验证 MateCat/MyMemory props、未知 prop 保留、context/provenance 导入、受控 export、fixture/round-trip，以及可选 TMX ResourcePackage profile 的 payload/capability 合同。
- **Out**: ResourcePackage container/manifest authority、资源 apply transaction、provider 传输、MyMemory 在线 API、所有厂商私有扩展、复杂内联标签的首批编辑、项目文档打开。

## Boundary Candidates

- TMX syntax/profile；
- vendor prop registry；
- canonical TM record adaptation；
- export capability matrix；
- 导入警告与 provenance 展示。

## Out of Boundary

- 不假定每个 MateCat TMX 都有前后文；
- 不从 `speaker "text"` 外壳猜测任意角色语义；
- 不拥有 fuzzy 算法。

## Upstream / Downstream

- **Upstream**: Parser 的安全 XML/流式错误语义；SQLite canonical TM records。
- **Downstream**: context-aware suggestions、TMX exchange/export；可选地由 `language-resource-portability` 将已批准的 TMX payload profile 封装进 ResourcePackage。

## Existing Spec Touchpoints

- **Extends**: 已完成的 TMX Level 1 导入。
- **Adjacent**: `tm-storage-retrieval-index` 拥有存储和排序，本规格只负责互操作映射。

## Constraints

拒绝 DTD/ENTITY 的安全基线不回退；只对真实 fixture 验证过的属性作语义映射；未知属性不得无提示丢失。
