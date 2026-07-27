# 设计约束清单 — Qt 专业编辑器 MVP

## 设计来源

- 用户要求：先计划后实施，在 `feature/logic-excel-adapter` 最新进展上构建 PySide6 MVP，并最终位于 `ui-mvp` 分支。
- 参考框架：MateCat 编辑器截图、官方语言资源指南与公开源码仓库。
- 整体边界：交付一个真实可运行、完全本地的个人翻译桌面闭环，而不是静态原型。

## 各步骤硬性要求

| 步骤 | 硬性要求 | 验证锚定句 | 当前状态 |
|------|---------|-----------|---------|
| Step 1 | Qt 无关的数据契约、项目读写、资源注册和原子导入 | 完成后应能在无 Qt 环境中打开/保存项目并导入 TMX、CSV/XLSX，失败不损坏原资源 | 已验证 |
| Step 2 | 独立 EditorController 维护会话并协调现有引擎，旧 LogicController 三态不变 | 完成后应能通过 Logic 层查询 TM/术语、确认段落并按 Lookup/Update 配置读写资源 | 已验证 |
| Step 3 | PySide6 主编辑器具备双栏当前段、段落导航、建议页签、确认与保存 | 完成后应能从示例或真实 JSON/TXT 项目完成至少一个段落的编辑与确认 | 已验证 |
| Step 4 | 齿轮设置具备资源列表、新建、启停、Lookup/Update、TMX 和术语表导入 | 完成后应能从设置导入资源，回到编辑器后立即得到新建议 | 已验证 |
| Step 5 | 回归、offscreen GUI、真实导入与确认闭环验证 | 完成后应看到核心自检全绿、主窗口可启动、导入资源能被查询、确认译文能持久化 | 已验证 |

## 依赖

**Critical Path:** Step 1 → Step 2 → Step 3/4 → Step 5

**隐性依赖：**

- Step 3 的建议页签依赖 Step 2 已能同时查询 TM 与术语，而不是复用旧三态接口后把其中一个页签留空。
- Step 4 的“导入成功”依赖 Step 1 已执行原子资源替换和结构化统计，并依赖 Step 2 热重载资源；仅弹出文件选择器不算完成。
- Step 5 的“可运行”依赖真实 Qt 主窗口到达首个可用状态；只通过纯模型测试不算 GUI 验收。

## 设计红线

- PySide6 只能出现在 Layer 4 文件，Engine 不得导入任何 Qt 类型。
- Frontend 只调用 EditorController，不得直接调用 TMEngine、GlossaryEngine 或资源存储。
- `LogicController.get_suggestions()` 的 `TM_HIT / TERMS_FOUND / NO_MATCH` 契约、TM 优先规则和默认路径不得改变。
- 记忆库、术语表、设置导入和确认写回均为当前 MVP 核心能力，不得以“后续再做”降级。
- 不放置无功能的 QR、QA、聊天、共享、罚分或云端按钮。
- 不复制 MateCat logo、商标、CSS/React 源码或品牌图标。
- 遗留 `modular-cat-architect` 只作为历史参考，不得覆盖最新 steering、当前分支实际契约或本规格。
- README 必须在验收后更新，并保留用户已有的 Feature 3 标签与推送状态修改。
- 现有用户未提交改动不得被覆盖、回滚或混入 MVP 选择性提交。

## 降级决策记录

> 未经记录的降级视为越权。

| 时间 | 步骤 | 被降级的能力 | 原因 | 用户确认 |
|------|------|-------------|------|---------|
| （空） | | | | |

## 集成层验收检查

- [x] 使用 EditorController 的真实查询、确认、导入与热重载接口验收，而非只构造窗口
- [x] 使用真实临时 TMX/CSV/XLSX 与 JSON/TXT 项目完成闭环
- [x] 修正并运行旧 LogicController 自检，确认 Excel 依赖的三态契约未回归

## Steering 同步检查需求

- [x] `structure.md`：已新增 Editor Logic、资源导入与 Qt Frontend 组件。
- [x] `tech.md`：已记录 PySide6 6.11.1、依赖诊断和 UI 测试/启动命令。
- [x] `product.md`：Feature 4 已从计划更新为 MVP 已交付。
