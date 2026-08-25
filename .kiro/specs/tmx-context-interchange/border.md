# tmx-context-interchange 设计红线

## Critical Path

```text
Parser safe TMX reader
  → prop-preserving semantic import + deterministic writer
    → Resource / Workspace / one selected Chunk exact scope adapters
      → direct preview/publication
        → managed-resource-only ResourcePackage successor profile
          → Controller/Qt/current-source acceptance
```

## 红线

| 约束 | 验证锚点 |
|---|---|
| Source scope / payload / carrier 正交 | capability matrix；project/chunk direct-only |
| Workspace 拥有项目正文/顺序/presence | session view + universe projection exact binding |
| Chunk 只拥有一个明选分工 membership | one `ChunkScopeProjection`；无 current-row/path/text 猜测 |
| Resource 是完整 canonical snapshot | generation/revision/digest/count capture + revalidate |
| Parser 是唯一安全 reader | DTD/ENTITY/inline/limit hostile matrix |
| Unknown prop 不静默丢失 | type/lang/value/order/duplicates round-trip |
| Locale 不由 writer 猜测 | preview-bound effective locale；`und` UI default only |
| Empty target/detached 显式排除 | stable loss counts；missing/foreign/stale blocking |
| Direct artifact 不复用 ProjectPackage/ResourcePackage 语义 | 独立 preview/receipt/error；carrier-neutral I/O only |
| ResourcePackage 继续拥有 container/apply/receipt | injected TMX payload handler；new exact triple |
| 项目/分工永不进入 ResourcePackage | architecture + capability negative tests |
| Qt 不解析 TMX/scope/package/store | Controller typed command guard |

## 不得降级

不得把“先只做 managed resource TMX”或“先只做项目导出”当作完整交付；不得省略 selected chunk、direct publication recovery、unknown prop、ResourcePackage successor profile 或 Qt 明确入口。若任何一项因现有 owner seam 缺失，需要先补 seam，而不是把原始目标改写为后续计划。
