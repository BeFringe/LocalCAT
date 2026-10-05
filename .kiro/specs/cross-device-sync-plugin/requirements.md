# 需求文档

## 简介

LocalCAT 为个人译者提供显式、可禁用的跨设备包同步。用户使用自己的 R2 或 InfiniCLOUD WebDAV，在操作前检查计划，发生冲突时保留双方，并通过本地项目/资源事务继续工作。本次不提供内容加密或自动后台同步。

## 边界说明

- **范围内**：R2 先行、随后 InfiniCLOUD WebDAV；连接配置、凭据遮蔽/显式显示；手动上传/下载/双向计划、删除保护、冲突及恢复。
- **范围外**：内容加密、定时同步、账号系统、实时协作、翻译语义合并、chunk 权限搬运、live SQLite/journal/stage 同步。
- **相邻期望**：先完成 RPY 与手工包闭环；ProjectPackage 和 JSONL/CSV ResourcePackage 各自保有验证、导入、资源选择和 receipt 权威。

### Scope Lineage

- **Owning spec**：`cross-device-sync-plugin`。
- **被修订的既有范围说明**：产品的纯本地默认行为通过显式可选同步扩展；Multi/Resource 的网络传输延期项仅在各自批准的包出口之外编排。
- **相邻规格 / 契约**：`multi-document-project-workspace`、`language-resource-portability`、`rpy-project-codec`、平台文件/秘密后端、Qt Controller。
- **审批状态**：用户已选择 R2 先行、InfiniCLOUD WebDAV 后续、不加密、凭据默认遮蔽且可显示，并要求自动生成 R/D/T；新传输/持久状态/依赖边界与相邻 owner amendment 待独立治理闭合，尚未授权运行实现。

## 需求

### Requirement 1：显式配置与禁用

**目标：** 作为译者，我希望只有在主动配置和执行时才使用远端。

#### 验收标准
1. When 用户启用同步, the LocalCAT shall 允许配置 R2 的 endpoint/bucket/prefix 或 InfiniCLOUD 的 WebDAV URL/账户与远程目录，并显示尚缺信息。
2. While 同步未启用或已禁用, the LocalCAT shall 不建立网络连接，保持本地项目及资源完整可用。
3. When 用户手动测试连接或同步, the LocalCAT shall 只访问所选连接与范围，并明确显示该动作及结果；本次不自动定时执行。
4. If 连接配置变更, the LocalCAT shall 使旧计划失效并要求重新预览，不把一个远端的基线用于另一远端。

### Requirement 2：凭据交互与秘密保护

**目标：** 作为译者，我希望能检查自己的连接信息，同时避免无意暴露秘密。

#### 验收标准
1. When 凭据字段显示时, the LocalCAT shall 默认以星号或等价密码遮蔽显示，并提供带明确标签的显示/隐藏按钮。
2. When 用户显式点击显示, the LocalCAT shall 仅在当前设置视图显示该字段；隐藏、关闭或重新打开设置时恢复遮蔽，不自动复制秘密。
3. When 用户保存连接, the LocalCAT shall 将秘密与普通连接设置分开存放；支持使用本机环境变量提供 R2 凭据而不自动持久化其值。
4. The LocalCAT shall 不将秘密写入项目、资源包、错误、日志、测试 fixture、Git 或同步对象；不提供内容加密选项，传输仍使用校验证书的 HTTPS。

### Requirement 3：只同步完成的包

**目标：** 作为译者，我希望传输的是可使用的完整项目和资源。

#### 验收标准
1. When 用户选择上传, the LocalCAT shall 只接受对应 owner 已完成的 ProjectPackage 或 JSONL/CSV ResourcePackage，不扫描整个工作目录。
2. When 下载完成, the LocalCAT shall 先验证完整性及包内容，再创建本地导入预览；下载成功不等于已经应用。
3. If 本地资源或项目目标需要选择或替换, the LocalCAT shall 要求本地明确选择并重新验证，不凭远端 ID/receipt 自动授权。
4. The LocalCAT shall 不运输设备私钥、Fuzzy 资格、origin 绝对路径、活跃数据库、事务日志、临时文件或协作权限。

### Requirement 4：计划、模式与基线

**目标：** 作为译者，我希望在传输前理解将发生的变化。

#### 验收标准
1. When 用户预览手动 push、pull 或 two-way, the LocalCAT shall 显示逐项的新增、更新、下载、删除、无变化和冲突，并标明项目/资源及大小。
2. When 预览时存在共同基线, the LocalCAT shall 比较 base/local/remote 的内容事实，不按修改时间静默判定胜者。
3. If 本地、远端、配置或目标生命周期在预览后变化, the LocalCAT shall 拒绝过期操作并重新预览，不覆盖变化。
4. If 没有共同基线或无法确认完整列举, the LocalCAT shall 不将缺失自动解释为删除，不推进虚假基线。

### Requirement 5：冲突与删除保护

**目标：** 作为译者，我希望并发工作不会丢失，破坏性操作必须可见。

#### 验收标准
1. If 本地和远端从共同基线发生不同变化, the LocalCAT shall 保留双方，默认不应用覆盖，并允许用户重新预览后明确选择保留本地、采用远端或保留副本。
2. When 用户批准覆盖或删除, the LocalCAT shall 校验最新版本条件、显示影响范围，并保留可恢复的先前包内容；批量删除超过保护阈值时要求额外确认。
3. If provider 无法可靠执行所需条件操作, the LocalCAT shall 禁止受影响的覆盖/删除，明确说明限制，不回退为无条件写入。
4. When 远端显示已删除, the LocalCAT shall 将本地删除建议与活跃项目/资源管理分离，不直接删除已打开项目或已注册资源。

### Requirement 6：有界传输与可恢复执行

**目标：** 作为译者，我希望网络故障、取消与重启不会把不完整状态当成完成。

#### 验收标准
1. While 网络传输运行, the LocalCAT shall 显示可取消的进度并保持编辑/保存可用，按声明限制控制对象大小、列举规模和重试。
2. If 传输截断、摘要不符、认证失败或超时, the LocalCAT shall 给出安全诊断并保留本地状态；对可能已完成的写入先复证再重试。
3. When 一个同步项完成, the LocalCAT shall 仅在该方向所需的传输确认及本地 owner 提交完成后推进该项基线；其他失败项保留原状态。
4. When 应用重启或插件被禁用, the LocalCAT shall 停止旧会话继续提交；恢复时区分可复证完成、待处理、冲突和需 owner 恢复，不把多个包描述为全局原子事务。

### Requirement 7：R2 与 InfiniCLOUD provider 验收

**目标：** 作为译者，我希望被列为支持的 provider 具有实际验证过的行为。

#### 验收标准
1. When 首批交付 R2, the LocalCAT shall 验证指定 prefix 内的列举、读取、创建、条件更新、受保护的逻辑删除以及重试结果，不承诺所有 S3-compatible 服务均兼容。
2. When 接入 InfiniCLOUD WebDAV, the LocalCAT shall 使用用户实际分配的 DAV URL 与独立连接凭据，按服务限制列举，并在能力验证通过后才开放写同步。
3. If 某项 provider 能力尚未验证或服务器返回不兼容语义, the LocalCAT shall 显示未验证/不支持状态，保留安全的只读或导出途径，不宣称完整同步通过。
4. When 进行真实环境验收, the LocalCAT shall 只在明确的测试前缀使用合成包，证明两设备冷重开、冲突、失败恢复与凭据脱敏，不改动用户无关远端内容。

### Requirement 8：一致的桌面工作流

**目标：** 作为译者，我希望同步使用清晰的本地产品界面。

#### 验收标准
1. When 用户打开同步设置或计划, the LocalCAT shall 显示连接状态、选定包、操作和阻断原因，不暴露包内部 manifest、token 或执行权限。
2. When 用户取消计划或拒绝导入, the LocalCAT shall 保持当前项目、资源选择和未保存编辑，不自动替换活跃会话。
3. When 下载的 RPY 项目缺失对应 codec, the LocalCAT shall 保持 Project 的中立编辑/包保存能力，明确禁止 TL 导出。
4. When 完成首个同步版本, the LocalCAT shall 提供 R2 完整手动旅程和 InfiniCLOUD 的实际能力结果，不以模拟 provider 测试替代真实 endpoint 验收。
