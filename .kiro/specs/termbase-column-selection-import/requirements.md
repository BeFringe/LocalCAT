# 需求文档

## 简介

LocalCAT 的 Parser 已支持 CSV/XLSX 术语表按列名或物理列序号显式选择 source/target，但现有 Controller/Qt 导入仍固定使用前两列兼容 preset。本文闭合一个窄产品切片：用户选择术语文件后，由格式 codec 在安全 sealed snapshot 上预览 CSV 首个逻辑 record 或 XLSX active worksheet 首行的候选列；用户明确选择 source/target 列和表头模式后，既有导入事务在同一 source identity 上执行。

本需求不让 Qt、Controller 或 Store 读取 CSV/XLSX grammar。preview、header matching、active-sheet 选择和正式 row parsing 始终由同一个 termbase codec authority 拥有。

## 边界说明

- **范围内**：CSV/XLSX codec-owned 列 preview；有界列候选；active sheet 显示事实；source/target 物理列选择；首行作为表头或数据；preview→import 内容身份绑定；Controller typed command；Qt 非阻塞 preview/确认；前两列兼容 preset；既有原子术语提交与结构化报告。
- **范围外**：按项目源/目标语言自动猜列；多 Sheet 聚合或 Sheet 选择；任意 Excel 项目导入；术语导出/ResourcePackage；TMX；PO/POT；多文档；改变 TermbaseStore LWW/事务；让 Qt/openpyxl 复制 parser grammar。
- **相邻期望**：未来 `language-resource-portability` 可消费本规格的显式列选择和 import receipt 事实；`multi-document-project-workspace` 不消费术语列 preview；Parser 仍不拥有 Qt/Controller/Store authority。

### Scope Lineage

- **Owning spec**：`termbase-column-selection-import`
- **被修订的既有范围说明**：`parser-subsystem-extraction` 已冻结 `TermbaseColumnSelection` 与前两列兼容 preset，但把 Qt/Controller 列选择器留给未来 consumer；`feature5-ui-integration` 已冻结资源设置和异步导入面。
- **相邻规格 / 契约**：`parser-subsystem-extraction`、`feature5-ui-integration`、`qt-editor-json-mvp-increment`
- **审批状态**：用户于 2026-08-21 明确批准直接设计 R/D/T 并实施 codec-owned 安全 preview 与术语列选择，不新建 worktree。

## 需求

### Requirement 1：Codec-owned 安全列预览

**目标：** 作为术语导入用户，我希望在导入前看到文件实际提供的列，以便不依赖默认前两列选择正确的 source 和 target。

#### 验收标准

1. When 请求 CSV 或 XLSX 术语列 preview 时，the Parser Application surface shall 在既有 safe-root、regular-file、input limit 与 sealed snapshot 边界内选择对应 termbase codec。
2. The preview codec shall 复用正式导入的 UTF-8/CSV logical-record grammar 或 XLSX OPC preflight、active-sheet 与 worksheet-row 读取语义，不得建立第二套 header parser。
3. When CSV 首个逻辑 record 或 XLSX active worksheet 首行存在时，the preview shall 按物理列顺序返回有界的零基列索引与可选 header candidate，并保留重复、空值与单格文本截断的可观察事实。
4. When 预览 XLSX 时，the preview shall 只读取 active worksheet，并返回有界 active sheet 显示名；不得聚合、推断或授权其他 Sheet。
5. If 输入为空、编码无效、XLSX preflight 失败、active worksheet 缺失或条件依赖不可用，the preview shall 返回稳定 body-safe failure，不得返回可用于导入的 preview。
6. The preview shall 绑定实际 sealed `SourceSnapshotIdentity`、FormatId 与 codec identity；header candidate 属于有界用户内容，不得进入错误摘要或日志。
7. The preview shall 最多保留 256 个列候选、每个候选最多 256 个 Unicode 字符，并发布实际首行列数与 truncation 状态。

### Requirement 2：显式列与表头选择

**目标：** 作为术语导入用户，我希望明确指定 source/target 列及首行用途，以便导入非默认列布局而不丢失第一条术语。

#### 验收标准

1. When 用户确认 preview 时，the UI shall 提交不同的 source/target 零基物理列索引及 `FIRST_ROW` 或 `NO_HEADER` 模式。
2. If source 与 target 选择同一列、列索引为负、选择超出实际首行列数或 preview 已截断且选择不可见，the request shall 在导入前失败。
3. When 模式为 `FIRST_ROW` 时，the codec shall 使用既有 `_resolve_columns`/row-selection authority 消费首行，并从第二物理行开始生成术语记录。
4. When 模式为 `NO_HEADER` 时，the codec shall 把首行作为数据，并保留物理 row ordinal 与 warning holes。
5. The Qt selector shall 默认选择第 1/2 列，并根据 codec-owned legacy-header detection 设置初始表头模式；用户仍可显式修改。
6. The 系统 shall 不按项目 locale、列名语言、常见别名或内容统计自动替换用户的列选择。
7. Where 既有 Application 调用未提供显式选择时，the importer shall 继续使用 source index 0、target index 1 与 `LEGACY_ALLOWLIST`，保持现有兼容行为。

### Requirement 3：Preview 与正式导入的内容身份闭合

**目标：** 作为本地数据用户，我希望预览后导入的是同一份文件内容，以便外部改写不会使列选择应用到另一份数据。

#### 验收标准

1. When Qt 由 preview 构造显式 import request 时，the request shall 携带 preview 的完整不可变 source identity（包含 content SHA-256 与 byte count），不携带可复用文件句柄或绝对内部路径证明。
2. Before 正式 parser stream 产生记录时，the Application importer shall 精确比较新 sealed snapshot 与 preview identity，并在同一 sealed snapshot 上重新取得 codec preview、核对可见列数；身份不一致时返回 `PARSER.SOURCE.STALE`，列事实不一致时拒绝选择。
3. If preview 后文件内容变化、文件被替换或重新选择不同路径，the importer shall 不调用 TermbaseStore prepare/commit，且原资源保持不变。
4. When 内容身份一致且 parser verified terminal 成功时，the Controller shall 把 accepted rows 一次性交给既有 TermbaseStore transaction；preview 本身不得写入资源。
5. If parser、consumer 或 commit 任一阶段失败，the Controller shall 保留原资源、runtime 与 LKG，并返回既有 ImportReport failure shape。

### Requirement 4：Controller 与 Qt 非阻塞消费面

**目标：** 作为桌面端译者，我希望在资源设置中安全选择术语列，同时大文件预览不冻结界面。

#### 验收标准

1. When 用户为术语资源选择 CSV/XLSX 文件时，the Qt settings dialog shall 先启动非 GUI 线程 preview，完成前禁用冲突的资源操作并显示进度反馈。
2. When preview 成功且至少有两个可选择列时，the Qt shall 显示 source/target 选择器、表头复选框、文件格式和 active sheet（如适用）。
3. If 用户取消列选择，the Qt shall 不启动 import、不改变资源且恢复可交互状态。
4. When 用户确认时，the Qt shall 通过一个 typed `ImportRequest` 调用既有 `EditorController.import_resource()`，不得直接调用 Parser、CSV 或 openpyxl。
5. If preview 或 import 已在运行，the Qt shall 拒绝第二个并发 preview/import，并在任务结束后释放 worker。
6. The Qt shall 继续为 TMX 使用既有 locale 对话，不向 TMX 显示术语列选择。
7. The import feedback shall 继续显示 imported/skipped/overwritten/errors，不回显未选列正文或完整 header 内容。

### Requirement 5：合同有界性与架构边界

**目标：** 作为维护者，我希望 preview 是中立、可注入且有界的 codec capability，以便内建和未来 provider 不能绕过 Parser Foundation。

#### 验收标准

1. The `CodecCapabilities` shall 显式声明 termbase column preview；只有声明该能力的 descriptor 可由 Application surface 请求 preview。
2. When reader factory 声明 preview capability 时，the registry shall 验证结构式 preview protocol、固定 descriptor authority 并屏蔽 factory property/exception 正文。
3. If descriptor 未声明 preview、factory 缺少 behavior、返回错误类型或 report identity 与选中 descriptor/snapshot 不一致，the Application shall fail closed，且不触碰 Store。
4. The Parser preview contracts shall 只依赖 stdlib-only neutral types，不导入 Qt、Controller、Editor、Store 或 openpyxl 类型。
5. The XLSX codec shall 仅在既有 OPC/XML preflight 后条件加载 openpyxl，并保持 `read_only=True`、`data_only=True`、`keep_links=False`、`keep_vba=False`。
6. The architecture tests shall 证明 Qt/Controller 不导入具体 codec，composition 仍是内建 codec 唯一注册点，preview 不产生第二 grammar owner。

### Requirement 6：兼容与完成验证

**目标：** 作为现有 LocalCAT 用户，我希望新列选择不破坏前两列导入、术语 CRUD 或其他资源行为。

#### 验收标准

1. The 既有 CSV/XLSX 前两列 import、header allowlist、source-LWW、overwrite count、atomic replace、term reload/LKG 与 Qt import tests shall 保持通过。
2. The 新测试 shall 覆盖 CSV/XLSX preview、空/重复/长 header、256 列 truncation、active sheet、dangerous XLSX、stale content、同列拒绝、FIRST_ROW/NO_HEADER 和 compatibility preset。
3. The fault tests shall 证明 preview 不提交、stale 不提交、parser fatal 不提交、consumer/commit failure 不提交。
4. The fresh completion shall 运行 Parser contracts/registry/composition/termbase codec、resource importer、Controller term import、Qt settings import 与 architecture guards。
5. The Steering sync shall 只记录已落地的术语显式列 preview/selection，不声称多 Sheet、自动语言匹配、ResourcePackage 或项目 XLSX 已实现。
