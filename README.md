# LocalCAT

LocalCAT 是一款轻量级、模块化、本地优先的计算机辅助翻译（CAT）工具。

## 🌟 核心愿景
针对商业 CAT 工具日益严重的商业化限制（额度限制、隐私风险、免费用户负优化），LocalCAT 旨在提供一个完全受控的本地化翻译环境。

- **100% 本地化**：翻译记忆库（TM）与术语表（Glossary）均存储在本地，不强制联网。
- **模块化设计**：核心逻辑与 UI 完全解耦，支持从简单的 Excel 协作过渡到专业的 QT 界面。
- **高性能**：采用前缀树（Trie）等高效算法处理超大规模语料。

## 🏗 系统架构
LocalCAT 遵循严格的四层架构设计：
1. **Layer 1 - Storage (持久层)**: 负责 JSONL/SQLite 数据存储
2. **Layer 2 - Core Engine (核心引擎)**: 处理术语提取、TM 匹配与文件解析
3. **Layer 3 - Logic UI (交互逻辑层)**: 状态维护与 UI 适配
4. **Layer 4 - Frontend (表示层)**: Excel Adapter / QT Desktop

详细架构设计请参考 [spec.md](spec.md)

## 🚀 开发里程碑 (Development Roadmap)

| Feature | 架构层次 | 状态 | Git 标签 | 核心文件 |
|---------|---------|------|---------|---------|
| **Feature 1: 术语表引擎** | Layer 2 | ✅ 已完成 | v0.1.0-feature1 | `glossary_engine.py` |
| **Feature 2: 翻译记忆库引擎** | Layer 1 + Layer 2 | ✅ 已完成 | v0.2.0-feature2 | `tm_engine.py`, `tm.jsonl` |
| **Feature 3: 逻辑层与 Excel 适配器** | Layer 3 + Layer 4 (Excel) | ✅ 已完成 | v0.3.0-feature3 | `logic_controller.py`, `excel_adapter.py` |
| **Feature 4: QT 专业编辑器** | Layer 4 (QT) | 🔮 计划中 | - | - |
| **Feature 5: 模糊匹配与自动化** | Layer 2 增强 | 🔮 计划中 | - | - |

### Feature 1: 术语表引擎 (Glossary Engine)
- **架构层次**: Layer 2 (Core Engine)
- **实现内容**:
  - 基于 Trie 的高性能术语提取逻辑
  - 支持重叠匹配与长词优先策略
- **核心文件**: `glossary_engine.py`
- **Git 信息**: 分支 `feature/tm-engine`, 标签 `v0.1.0-feature1`

### Feature 2: 翻译记忆库引擎 (TM Engine)
- **架构层次**: Layer 1 (Storage) + Layer 2 (Core Engine)
- **实现内容**:
  - 100% 精确匹配与追加式 JSONL 持久化存储
  - 支持 PO/JSON 文件导入
  - 系统集成压力测试验证
- **核心文件**: `tm_engine.py`, `tm.jsonl`, `stress_runner.py`
- **Git 信息**: 分支 `feature/tm-engine`, 标签 `v0.2.0-feature2`

### Feature 3: 逻辑层与 Excel 适配器 (Logic Layer & Excel Adapter)
- **架构层次**: Layer 3 (Logic UI) + Layer 4 (Frontend - Excel)
  - Layer 3: `logic_controller.py` - 无状态逻辑转发
  - Layer 4: `excel_adapter.py`, `excel_adapter_openpyxl.py` - Excel 前端适配
- **实现内容**:
  - 无状态逻辑转发层（TM 优先策略）
  - Excel 手动触发工作流（xlwings 交互式 + openpyxl 文件模式）
  - 性能基准测试（5/50/200/800 行，冷/热启动，分段计时）
- **核心文件**: `logic_controller.py`, `excel_adapter.py`, `excel_adapter_openpyxl.py`
- **Git 信息**: 分支 `feature/logic-excel-adapter`, 标签 `v0.3.0-feature3`

### Feature 4: QT 专业编辑器 (QT Desktop Editor) 🔮
- **架构层次**: Layer 4 (Frontend - QT)
- **计划内容**:
  - PySide6 双栏对比翻译界面
  - 快捷键系统、标签处理、段落导航
  - 复用 Layer 1-3 的所有代码
- **Git 信息**: 分支 `feature/qt-desktop` (未创建)

### Feature 5: 模糊匹配与自动化 (Fuzzy Matching & Automation) 🔮
- **架构层次**: Layer 2 (Core Engine) 增强
- **计划内容**:
  - Levenshtein/Dice 系数模糊匹配
  - Docker 容器化部署
  - 协作翻译功能

## 🚧 当前状态
- **最新稳定版**: Feature 2 (v0.2.0-phase2) - 核心引擎完成
- **开发分支**: Feature 3 (feature/logic-excel-adapter) - 逻辑层与 Excel 适配器已完成，待推送
- **下一步**: Feature 4 (QT 专业编辑器) 规格设计

## 🛠 开发方法论
本项目采用 **AI 辅助架构驱动开发流 (MCA: Modular-CAT-Architect)**。每一阶段均经过“架构定义 -> 契约冻结 -> 隔离实现 -> 可见验证”的严格闭环。
