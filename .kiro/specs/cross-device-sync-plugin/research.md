# 研究与设计决策

## Summary

- **Feature**：`cross-device-sync-plugin`。
- **Discovery Scope**：新网络能力与既有 package owner 的复杂集成。
- **Key Findings**：R2 提供条件 PUT；Remotely Save 的适配器可参考但不能直接继承并发合同；InfiniCLOUD 需要对实际节点验证强条件行为；密码掩码是 UI 行为，不是内容加密。

## Research Log

### Remotely Save 与 R2

- **Sources Consulted**：[统一适配器](https://github.com/remotely-save/remotely-save/blob/master/src/fsAll.ts)、[S3 provider](https://github.com/remotely-save/remotely-save/blob/master/src/fsS3.ts)、[WebDAV provider](https://github.com/remotely-save/remotely-save/blob/master/src/fsWebdav.ts)、[R2 S3 API](https://developers.cloudflare.com/r2/api/s3/api/)、[R2 consistency](https://developers.cloudflare.com/r2/reference/consistency/)。
- **Findings**：Remotely Save 将远端读写分开，但公开写入接口没有 expected ETag；其 WebDAV 覆盖上传不构成 CAS。R2 文档列出 PutObject 的 If-Match/If-None-Match；强一致性本身不解决并发覆盖。
- **Implications**：独立实现条件版本协议，immutable object 完成后 CAS 提交 head；不复制第三方实现，不用 mtime/LWW。实际 R2 endpoint/bucket/prefix 是连接配置，key/secret 本身不足以组成连接。

### InfiniCLOUD 与 HTTP 条件

- **Sources Consulted**：[WebDAV 开发规范](https://infini-cloud.net/en/developer_webdav.html)、[认证规范](https://infini-cloud.net/en/developer_auth.html)、[Apps Password](https://infini-cloud.net/en/support_account_login-settings_appspass.html)、[URL 设置](https://infini-cloud.net/en/support_service_webdavurl.html)、[RFC 9110 §13.1.1](https://www.rfc-editor.org/rfc/rfc9110.html#section-13.1.1)、[RFC 4918 §8.6](https://www.rfc-editor.org/rfc/rfc4918.html#section-8.6)。
- **Findings**：服务使用账户分配的 DAV URL、User ID 与 Apps Password；不支持 Depth infinity。公开文档没有充分承诺实际节点的强 ETag/原子条件更新。HTTP If-Match 需要 strong comparison，弱 ETag 不足。
- **Implications**：Depth 1 有界列举、逐项 multistatus 检查。只读连通性与会创建隔离对象的写能力验证分开；条件竞争验证通过才开放写同步。用户个人 URL/用户名不写进通用规格。

### 凭据显隐与最小依赖

- **Sources Consulted**：[Kiss Translator SyncSetting](https://github.com/fishjar/kiss-translator/blob/dev/src/views/Options/SyncSetting.js)、[botocore 教程](https://docs.aws.amazon.com/botocore/latest/tutorial/)、[Cloudflare Python 示例](https://developers.cloudflare.com/r2/examples/aws/boto3/)、[keyring 文档](https://keyring.readthedocs.io/en/latest/)、[defusedxml](https://github.com/tiran/defusedxml)。
- **Findings**：Kiss 的 secret 输入默认 password，眼睛按钮切换 text；用户名不是密码字段。botocore 可直接使用低层 S3 client，无需引入 boto3 资源层；keyring 有系统秘密后端，也允许第三方 backend，需限制选择。
- **Implications**：Qt 使用等价密码掩码/显隐按钮；不引入加密字段。采用可选 botocore/keyring/defusedxml，固定发行依赖并验证条件参数。明确环境来源 `R2_KEY`/`R2_SECRET`，不用默认 AWS 凭据链；本轮研究未读取值。

以上资料在 2026-10-05 查询；动态 upstream 页面用于说明接口依据，具体发行依赖由实现任务锁定，不能据此宣称真实账号验收通过。

## Architecture Pattern Evaluation

| 方案 | 优点 | 局限 | 结论 |
| --- | --- | --- | --- |
| 镜像工作目录 | 简单 | 泄漏设备/活跃事务状态，无法保留 owner 权威 | 拒绝 |
| 普通覆盖上传加 mtime 比较 | API 少 | 竞态丢更新，时钟不可信 | 拒绝 |
| 完整包 immutable objects + 条件 head | owner 分离、可复证、保留旧 bytes | 需真实条件能力及独立恢复状态；占用历史对象空间 | 采用 |
| 通用动态插件平台 | 扩展多 | 超出两个已知 provider 的需要，扩大执行信任面 | 不采用；只做显式可信 composition |

## Design Decisions

### Decision：只同步完整 artifact

Project 与 Resource 保持独立 artifact/receipt，Sync 仅统一流传输和安全结果。新增 Project 整包 port，不借用其成员或路径接口。下载必须交回 owner 验证、预览和 apply。

### Decision：逻辑删除与无加密

删除是条件提交 tombstone，旧 package bytes 保留；首版不做远端 GC，也不直接删除活跃本地资源。按用户选择，不提供内容加密；HTTPS 和系统秘密存储仍保留。星号显示不替代秘密保护。

### Decision：先 R2，后具名 WebDAV

R2 低层条件 PUT 为首个可验证实现；InfiniCLOUD 的 capability 为实际连接事实，未知时只读。只读连通性不能证明写安全。公共文档与 fake server 都不代替真实隔离前缀验收。

## Risks & Mitigations

- 网络超时已写入：operation ID 与 head 复证，再决定成功、冲突或重新预览。
- owner apply 完成但 base 未落盘：仅消费 owner 已有 pending recovery；缺少完成事实时停止自动 apply，经现有 open/validate/export 重新观察并由用户确认新预览，不假定存在冷 receipt 查询或补造旧成功。
- 凭据重定向/错误泄漏：同 origin HTTPS、受限错误投影、secret repr 遮蔽和 UI 生命周期测试。
- immutable 历史对象增长：明确首版无 GC，显示传输大小；后续回收需独立协议，不擅自删旧包。
- 新网络/持久状态边界：治理候选与 owner amendment 在 Design 公开，未闭合前不实施。

## References

- [ResourcePackage](../../../resource_package.py)：既有受限 artifact port。
- [ProjectPackage](../../../project_package.py)：导出/导入及拟新增整包出口。
- [ADR-018](../../steering/adr/adr-018.md)：provider 不成为项目 authority。
