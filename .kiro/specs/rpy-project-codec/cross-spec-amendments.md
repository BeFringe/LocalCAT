# 目录与 RPY 的跨规格合同

RPY 从单个 TL 扩展到嵌套章节，需要通用的目录选择、单文件项目包和受控格式导出。通用合同保留在下列 owner；本规格只解释 Ren’Py TL。依据 [ADR-030](../../steering/adr/adr-030.md)，RPY 手工包闭环先于同步。

| Owner 与现行合同 | 合同差异 | 唯一执行任务 |
| --- | --- | --- |
| [Parser Requirements 10](../parser-subsystem-extraction/requirements.md#需求-10能力声明与写入边界)；[中立准备与发布](../parser-subsystem-extraction/design.md#中立-round-trip-准备与发布) | 从 canonical-only 执行面扩展为可选 round-trip prepare 与一次性 prepared 发布；无格式私有类型、journal/LKG 或 Project authority | [RPY Tasks](tasks.md) 1.2、1.3、3.8 |
| [平台 Requirements 2、5](../windows-platform-enablement/requirements.md)；[目录合同](../windows-platform-enablement/design.md#只读目录观察与受控目标物化) | 增加 retained-root metadata 观察和 descendant target 准备/物化；不复用 retirement authority | [Multi Tasks](../multi-document-project-workspace/tasks.md) 5.1–5.4、5.9；[RPY Tasks](tasks.md) 3.5–3.8、5.3 |
| [Project Requirements 3、4、13](../multi-document-project-workspace/requirements.md)；[目录与单文件项目](../multi-document-project-workspace/design.md#显式根目录选择与单文件项目)；[private member](../multi-document-project-workspace/design.md#opaque-codec_private_member) | 根目录递归预览后勾选；单选仍保留原根和相对路径；配置 surface 在 verified terminal 后交付中立 records/source/opaque private member，包 schema 与事务仍归 Project | [Multi Tasks](../multi-document-project-workspace/tasks.md) 5.5、5.6、5.6a；[RPY Tasks](tasks.md) 1.4、3.2 |
| [Qt Requirements 10](../qt-editor-json-mvp-increment/requirements.md#requirement-10项目打开目录导航与格式导出)；[Controller/Qt 接缝](../qt-editor-json-mvp-increment/design.md#项目打开格式导出与手动同步视图) | 统一打开、相对目录导航、包保存与格式导出中立 view；保持 issued session/generation 与 Chunk guards | [Multi Tasks](../multi-document-project-workspace/tasks.md) 5.7、5.8、5.10；[RPY Tasks](tasks.md) 3.2a、3.4a、3.4b、5.1、5.2、6.2 |

单文件 TL 先复用 ProjectPackage；目录入口复用 Multi 的选择服务。RPY 不另建枚举器或包 authority，Project 不导入 RPY 语法。各任务按原 Tasks 依赖完成验证；跨规格引用不复制完成状态。
