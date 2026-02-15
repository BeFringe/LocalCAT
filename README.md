# LocalCAT

LocalCAT 是一款轻量级、模块化、本地优先的计算机辅助翻译（CAT）工具。

## 🌟 核心愿景
针对商业 CAT 工具日益严重的商业化限制（额度限制、隐私风险、免费用户负优化），LocalCAT 旨在提供一个完全受控的本地化翻译环境。

- **100% 本地化**：翻译记忆库（TM）与术语表（Glossary）均存储在本地，不强制联网。
- **模块化设计**：核心逻辑与 UI 完全解耦，支持从简单的 Excel 协作过渡到专业的 QT 界面。
- **高性能**：采用前缀树（Trie）等高效算法处理超大规模语料。

## 🏗 系统架构
LocalCAT 遵循严格的四层架构设计：
1. **Storage (持久层)**: 负责 JSONL/SQLite 数据存储。
2. **Core Engine (核心引擎)**: 处理术语提取、TM 匹配与文件解析。
3. **Logic UI (交互逻辑层)**: 状态维护与 UI 适配。
4. **Frontend (表示层)**: 
   - Phase 1-2: Excel Adapter (面向非编程人员)
   - Phase 3: PySide6 (QT) 专业编辑器

## 🚀 开发路线图 (Roadmap)
- [x] **Phase 1: 术语表引擎 (Glossary Engine)**
  - 实现基于 Trie 的高性能术语提取逻辑。
  - 支持重叠匹配与长词优先策略。
- [ ] **Phase 2: 翻译记忆库 (TM) 与 文件解析** (进行中)
  - 实现 100% 精确匹配与追加式持久化存储。
  - 支持 PO/Markdown 文件解析。
- [ ] **Phase 3: 专业 UI 界面 (Qt for Python)**
- [ ] **Phase 4: 模糊匹配与 Docker 部署**

## 🛠 开发方法论
本项目采用 **AI 辅助架构驱动开发流 (MCA: Modular-CAT-Architect)**。每一阶段均经过“架构定义 -> 契约冻结 -> 隔离实现 -> 可见验证”的严格闭环。
