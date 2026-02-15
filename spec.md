# LocalCAT Master Blueprint v1: 全局规格书

## 1. 系统架构与分层职责 (System Architecture)

本系统采用 **四层架构** 设计，旨在实现核心逻辑与用户界面的完全解耦，确保从 Excel 插件形态向独立 QT 桌面应用形态的平滑迁移。

### 1.1 Data Storage Layer (数据持久层)
- **职责**: 负责所有持久化数据的读写操作。
- **技术边界**:
  - 管理 **Glossary (术语表)**: 支持 CSV, XLSX, TBX 格式读取。建议使用 SQLite 或内存 Trie 结构加速查询。
  - 管理 **Translation Memory (TM, 记忆库)**: 采用 **Append-only (追加式)** 策略，推荐使用 JSONL (便于流式读写与版本控制) 或 SQLite。
  - 负责 **Project Settings**: 存储项目配置、用户偏好。
- **关键特性**:
  - **流式 I/O**: 针对超大文件 (GB级)，必须提供迭代器 (Iterator) 接口，严禁一次性加载全量文本到内存。

### 1.2 Core Engine Layer (核心引擎层)
- **职责**: 纯粹的业务逻辑与算法实现。
- **技术边界**:
  - **Term Extraction**: 实现高效的多模式匹配算法 (如 Aho-Corasick)，从 `SourceUnit` 中识别术语。
  - **TM Matching**: 实现模糊匹配 (Levenshtein/Dice coefficient) 和精确匹配逻辑。
  - **无状态 (Stateless)**: 引擎层不应持有 UI 状态，只处理输入并返回结果。
- **依赖约束**: 严禁引用 `xlwings`, `PySide6` 或任何 UI 相关库。

### 1.3 Logic UI Layer (交互逻辑层 / 胶水层)
- **职责**: 作为 "Glue" (胶水)，连接 Engine 与 Frontend，维护应用状态。
- **核心作用**:
  - **状态管理 (State Management)**: 维护当前活跃的项目、当前处理的 `SourceUnit`、用户的临时修改。
  - **适配器 (Adapter)**: 将 Engine 返回的通用数据结构 (`TermHit`, `TMMatch`) 转换为前端友好的格式。
    - *Excel 场景*: 将匹配结果格式化为单元格批注或右侧窗格的 HTML 内容。
    - *QT 场景*: 将匹配结果转换为 `QStandardItemModel` 或供组件渲染的信号。
  - **解耦策略**: 定义统一的 `IFrontendController` 接口，Excel 和 QT 分别实现该接口的后端逻辑。

### 1.4 Frontend Layer (表示层)
- **职责**: 用户交互与数据展示。
- **Phase 1-3 (Excel)**:
  - 利用 Excel 作为宿主，通过 `xlwings` 或 COM 接口与 Python 通信。
  - 侧重于利用 Excel 的网格编辑能力。
- **Phase 4+ (QT/PySide6)**:
  - 独立的桌面窗口应用。
  - 提供更专业的 CAT 界面 (双栏对照、标签处理、快捷键)。

---

## 2. 不可变数据契约 (Immutable Data Contracts)

所有跨层传递的数据对象必须是不可变 (Immutable) 的，建议使用 Python `dataclasses` (frozen=True) 或 `NamedTuple`。

### 2.1 SourceUnit (待译文本单元)
描述一个最小的翻译单元。

```python
from dataclasses import dataclass
from typing import Optional, Dict

@dataclass(frozen=True)
class SourceUnit:
    id: str                 # 唯一标识符 (Hash or Sequence ID)
    text: str               # 待译原文
    context_prev: Optional[str] = None  # 上文 (用于辅助理解)
    context_next: Optional[str] = None  # 下文
    speaker: Optional[str] = None       # 发言人/角色
    file_source: str = ""               # 来源文件名
    metadata: Dict = None               # 扩展字段 (如时间戳、标签)
```

### 2.2 TermHit (术语命中)
描述在原文中发现的一个术语匹配。

```python
@dataclass(frozen=True)
class TermHit:
    source_term: str        # 原文术语
    target_term: str        # 译文术语
    start_index: int        # 原文中的起始位置
    end_index: int          # 原文中的结束位置
    glossary_source: str    # 来源术语表名称
    definition: Optional[str] = None # 术语定义/备注
    priority: int = 1       # 优先级 (高优显示)
```

### 2.3 TMMatch (记忆库匹配)
描述一个来自 TM 的翻译建议。

```python
@dataclass(frozen=True)
class TMMatch:
    source: str             # TM 中的原文
    target: str             # TM 中的译文
    similarity: float       # 相似度 (0.0 - 1.0)
    match_type: str         # "EXACT", "FUZZY", "CONTEXT"
    tm_source: str          # 来源 TM 文件名
    usage_count: int = 0    # 使用次数 (可选)
    last_used: str = ""     # 最后使用时间 (ISO format)
```

---

## 3. 核心 API 接口定义 (Core API Definitions)

### 3.1 IEngine (Engine Interface)
Engine 层暴露给 Logic UI 的主要接口。

```python
from abc import ABC, abstractmethod
from typing import List

class IEngine(ABC):
    
    @abstractmethod
    def load_project_context(self, config_path: str) -> bool:
        """加载项目配置，初始化术语表和 TM 引擎"""
        pass

    @abstractmethod
    def get_suggestions(self, unit: SourceUnit) -> Dict[str, List]:
        """
        核心查询接口
        返回: {
            "terms": List[TermHit],
            "tm_matches": List[TMMatch]
        }
        """
        pass

    @abstractmethod
    def add_to_tm(self, unit: SourceUnit, translation: str) -> bool:
        """将确认的翻译写入 TM"""
        pass
```

### 3.2 ILogicController (Logic UI Interface)
Logic UI 暴露给 Frontend (Excel/QT) 的调用入口。

```python
class ILogicController(ABC):
    
    @abstractmethod
    def on_selection_change(self, text: str, row_index: int, sheet_name: str):
        """
        前端触发：用户选中某行或某个单元格
        逻辑：封装 SourceUnit -> 调用 Engine.get_suggestions -> 通知前端渲染
        """
        pass
        
    @abstractmethod
    def commit_translation(self, text: str, translation: str):
        """前端触发：用户确认翻译"""
        pass
```

---

## 4. Phase 1 (Glossary) 启动预览

**目标**: 实现高效的术语提取，并能在 Excel 中即时显示。

### 4.1 核心实现逻辑
1.  **Storage**: 编写 `GlossaryLoader`，读取 CSV/XLSX 文件，清洗数据。
2.  **Engine**: 
    - 采用 **Aho-Corasick 算法** (使用 `pyahocorasick` 库或纯 Python 实现的 Trie 树) 构建内存索引。
    - 实现 `TermExtractor` 类，输入 `text`，输出 `List[TermHit]`。
    - 确保算法复杂度为 O(n + m + z) (n=文本长度, m=模式总长, z=匹配数量)，而非简单的循环 replace，以应对长句和海量术语。
3.  **Logic UI**: 
    - 接收 Excel 当前选中单元格的文本。
    - 调用 Engine 获取 `TermHit`。
    - 格式化输出：生成一个简单的 HTML 字符串或纯文本列表 (如 `[Term] Source -> Target`)。
4.  **Frontend (Excel)**:
    - 绑定 `SelectionChange` 事件。
    - 调用 Python 函数，将 Logic UI 返回的字符串写入 Excel 的 "批注" (Comment) 或 任务窗格 (Task Pane)。

### 4.2 验证标准
- [ ] 能正确读取包含 1000+ 条目的术语表。
- [ ] 输入一段文本，能毫秒级返回所有匹配术语。
- [ ] 匹配结果包含准确的原文/译文和来源。
- [ ] 即使在 Excel 中高频切换单元格，Python 进程依然稳定，无内存泄漏。
