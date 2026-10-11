# OFD 锁的有限能力发现

日期：2026-10-12。仅独立 C 探针及本目录新建合成空文件；没有业务数据库或 SQLite 扩展加载。此记录不是完整 VFS 验收、平台资格或最低 OS 支持承诺。

## 官方资料

Apple 当前公开 XNU `fcntl.h` 在 `__DARWIN_C_LEVEL >= __DARWIN_C_FULL` 下定义 OFD 常量，不在 `PRIVATE` 分支。本机 SDK 26.5 的公开头及 fcntl 手册同样提供它们。没有查到可据此承诺首次 macOS 支持版本的 availability 标记；旧 Apple 头曾放在 PRIVATE，不能将当前主线倒推到旧系统。[Apple 公开头](https://raw.githubusercontent.com/apple-oss-distributions/xnu/main/bsd/sys/fcntl.h)

Apple 正式手册说明：锁属于 open file description；dup/fork 共享描述，显式解锁共同生效；最后引用关闭才自动释放；无死锁检测。查询不归属单个进程的锁时 `l_pid=-1`。[Apple fcntl 手册](https://raw.githubusercontent.com/apple-oss-distributions/xnu/main/bsd/man/man2/fcntl.2)

Linux man-pages 明确说明 OFD 与传统 record lock 互斥，即使同进程；不同 open descriptions 互斥，dup/fork 共享，最后关闭才释放。文档记载 Linux 3.15 起可用；文件系统仍须检测。[Linux OFD 文档](https://www.man7.org/linux/man-pages/man2/F_OFD_SETLKW.2const.html)

**设计推导：**可以限定为公开 SDK 编译支持、实际内核和目标卷能力门通过的平台。未知或语义不通过时拒绝此路线，不静默退回无锁/旧 pathname 连接；不应无证据指定最早 Mac 支持版本。此次未运行 Linux。

## 构建和结果

- macOS 26.2 arm64 / APFS，SDK 26.5；以 Apple clang 和公开 SDK 编译。
- `probe.c` 使用 SDK 原生 `struct flock`，不猜布局、不硬编码 OFD 数值。
- 编译：`clang -Wall -Wextra -Werror -std=c11 probe.c -o probe`。
- 执行：`probe <新建合成目录>/synthetic-lock-file`，以 `O_CREAT|O_EXCL` 创建文件。
- 所有竞争非阻塞；子进程 fork 后立即 exec，`CLOEXEC` 关闭继承的 fd，再独立 open。
- `results.jsonl`：退出 0，21 个检查符合预期；`stderr.log` 为空。原环境及构建记录保留在本地；分享件省去本机路径和二进制。

| 情况 | 实际结果 |
|---|---|
| 同 inode 的独立 open，OFD 写锁竞争 | 后者 EAGAIN，Mac errno 35 |
| 同进程 OFD 与传统 POSIX 写锁 | 互斥 |
| 新 exec 进程以 OFD/传统锁竞争父 OFD | 均互斥 |
| 关闭无关第三 fd | OFD 锁保留 |
| GETLK 查询另一个描述的写锁 | 写锁，owner -1 |
| dup 后关闭原 fd | 同进程/新进程仍受阻 |
| 最后 duplicate 关闭 | 锁释放 |
| 两个描述共享读锁 | 共存 |
| 共享锁升级独占 | 另一个读锁未释放则失败，释放后成功 |
| 父持传统锁，子进程 OFD 竞争 | 互斥 |
| 传统锁对照，关闭无关第三 fd | 锁消失，子进程可取得锁 |

## 不可外推的边界

只证明当前组合的基本字节区间锁语义；SQLite 的 SHARED/RESERVED/PENDING/EXCLUSIVE 状态、busy/retry、崩溃恢复、性能仍未实现或验证，W1 不能替代数据库锁。

OFD 不阻止目录 rename、非协作写入，也不提供 unlink 的 identity-CAS。DB/journal 绑定仍由 VFS 的 dirfd I/O 与 identity 管理负责。自定义 OFD 与默认 unix 传统锁互斥，不表示默认连接的传统锁自身免受其他 fd close 影响，因此尚不能允许同进程两种 VFS 任意混用。

**dup 不是新的锁主体。**若给多个 SQLite 连接 dup 同一个 authority fd，它们共享 open description，不会相互争锁。每连接应在 retained dirfd 下独立 open 并核对 inode，或另有严格本地锁状态/串行机制。fork 继承描述也能影响父进程锁；子进程应 exec 后重新创建 authority/连接。

资格必须针对实际资源卷；scratch APFS 通过不等于 USB/NFS/SMB/Linux 通过。这份观测不用于复用签发其他平台或设备资格。
