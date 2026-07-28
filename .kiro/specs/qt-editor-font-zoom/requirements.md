# 需求文档

## 简介

LocalCAT Qt 编辑器面向长时间处理双语文本的个人译者。当前编辑字号固定，用户不能通过常见的 Ctrl+滚轮手势临时调整可读性，重启后也没有可恢复的字号偏好。

本功能为编辑状态的 source/target 对照区域提供同步字号缩放，并把选择作为本机显示偏好保存。它不改变项目内容、浏览排版或应用其余界面的比例。

## 边界说明

- **范围内**：编辑状态 source/target 文本的 Ctrl+滚轮同步缩放、有限字号范围、当前会话即时生效和本地偏好恢复。
- **范围外**：浏览/校对页缩放、段落列表字号、全局 UI 缩放、字体族选择、逐项目字号、项目文件中的显示配置和新的搜索/替换能力。
- **相邻期望**：复用现有本地显示偏好的用户体验；字号状态不得写入可交换的翻译项目或语言资源。

## 需求

### Requirement 1：编辑字号缩放

**目标：** 作为个人译者，我希望用 Ctrl+滚轮同步调整编辑对照文字，以便在不同屏幕和阅读距离下保持 source/target 可读。

#### 验收标准

1. While 编辑工作区处于编辑状态, when 用户在 source 或 target 编辑区域按住 Ctrl 并向上滚轮, the LocalCAT Qt 编辑器 shall 把 source 与 target 的编辑文字同步放大一个字号步进
2. While 编辑工作区处于编辑状态, when 用户在 source 或 target 编辑区域按住 Ctrl 并向下滚轮, the LocalCAT Qt 编辑器 shall 把 source 与 target 的编辑文字同步缩小一个字号步进
3. When 编辑字号发生变化, the LocalCAT Qt 编辑器 shall 立即使用相同字号重新显示当前 source 与 target，且不要求切换段落或重启
4. When 用户只滚动滚轮而未按住 Ctrl, the LocalCAT Qt 编辑器 shall 保留编辑区域既有的滚动行为且不改变字号
5. When 编辑字号到达受支持的最小值或最大值, the LocalCAT Qt 编辑器 shall 保持边界字号且不继续越界
6. When 用户切换段落、项目或编辑/浏览模式后返回编辑状态, the LocalCAT Qt 编辑器 shall 保持当前会话已选择的编辑字号

### Requirement 2：本地字号偏好恢复

**目标：** 作为重复使用 LocalCAT 的译者，我希望字号选择在本机恢复，以便每次启动不必重新调整。

#### 验收标准

1. When 用户成功改变编辑字号, the LocalCAT Qt 编辑器 shall 将字号作为本地显示偏好保存
2. When 用户重新启动 LocalCAT 或创建新的主窗口, the LocalCAT Qt 编辑器 shall 恢复最近一次成功保存的编辑字号
3. The LocalCAT Qt 编辑器 shall 不把编辑字号写入翻译项目、翻译记忆库或术语表
4. If 本地字号偏好缺失、损坏或超出支持范围, the LocalCAT Qt 编辑器 shall 使用安全默认字号启动并保持编辑功能可用
5. If 本地字号偏好无法保存, the LocalCAT Qt 编辑器 shall 显示可理解的错误、保留当前可见字号并避免破坏此前有效的本地状态

### Requirement 3：显示范围与项目完整性

**目标：** 作为现有 LocalCAT 用户，我希望字号缩放只影响编辑阅读体验，以便项目内容和其他工作区布局保持稳定。

#### 验收标准

1. While 用户调整编辑字号, the LocalCAT Qt 编辑器 shall 保持 source、target、raw speaker、confirmed、段落身份、段落顺序和翻译记忆匹配身份不变
2. The LocalCAT Qt 编辑器 shall 不因编辑字号变化修改段落列表、浏览/校对页、菜单、设置对话框或其他应用控件的字号
3. When Ctrl+滚轮发生在 source/target 编辑区域以外, the LocalCAT Qt 编辑器 shall 不把该事件解释为编辑字号变更
4. The LocalCAT Qt 编辑器 shall 在本机处理并保存字号偏好，且不发送字号或项目数据到网络
