/* 仅合成文件的 OFD 能力发现；不是 SQLite VFS 或产品实现。 */
#define _GNU_SOURCE 1
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>
#ifdef __APPLE__
#include <sys/mount.h>
#endif

static int failed = 0;

static int attempt(int fd, int command, short type, off_t offset) {
    struct flock lock = {0};
    lock.l_type = type;
    lock.l_whence = SEEK_SET;
    lock.l_start = offset;
    lock.l_len = 1;
    lock.l_pid = 0;
    errno = 0;
    return fcntl(fd, command, &lock);
}

static void check(const char *label, int fd, int command, short type,
                  off_t offset, int success) {
    int rc = attempt(fd, command, type, offset);
    int error = errno;
    int busy = rc == -1 && (error == EAGAIN || error == EACCES);
    int ok = success ? rc == 0 : busy;
    printf("{\"test\":\"%s\",\"rc\":%d,\"errno\":%d,\"expected\":\"%s\",\"pass\":%s}\n",
           label, rc, error, success ? "success" : "busy", ok ? "true" : "false");
    fflush(stdout);
    if (!ok) failed++;
}

static void child_check(const char *executable, const char *path,
                        const char *label, const char *kind,
                        const char *expected) {
    pid_t child = fork();
    if (child == -1) { perror("fork"); exit(2); }
    if (child == 0) {
        execl(executable, executable, "child", path, label, kind, expected, (char *)NULL);
        _exit(127);
    }
    int status;
    if (waitpid(child, &status, 0) == -1) { perror("waitpid"); exit(2); }
    if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) failed++;
}

int main(int argc, char **argv) {
#if !defined(F_OFD_SETLK) || !defined(F_OFD_GETLK)
    puts("{\"supported_at_compile\":false}");
    return 77;
#else
    /* execl argv 包含 program + child + path + label + kind + expected。 */
    if (argc == 6 && strcmp(argv[1], "child") == 0) {
        int fd = open(argv[2], O_RDWR | O_CLOEXEC);
        if (fd == -1) { perror("child open"); return 2; }
        int command = strcmp(argv[4], "ofd") == 0 ? F_OFD_SETLK : F_SETLK;
        check(argv[3], fd, command, F_WRLCK, 0, strcmp(argv[5], "success") == 0);
        close(fd);
        return failed ? 1 : 0;
    }
    if (argc != 2) return 2;
    int a = open(argv[1], O_RDWR | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
    if (a == -1) { perror("create synthetic file"); return 2; }
    int b = open(argv[1], O_RDWR | O_CLOEXEC);
    int c = open(argv[1], O_RDWR | O_CLOEXEC);
    if (b == -1 || c == -1) return 2;
    printf("{\"environment\":{\"flock_size\":%zu,\"F_OFD_SETLK\":%d,\"F_OFD_GETLK\":%d}}\n",
           sizeof(struct flock), F_OFD_SETLK, F_OFD_GETLK);
#ifdef __APPLE__
    struct statfs fs;
    if (fstatfs(a, &fs) == 0) printf("{\"filesystem\":\"%s\"}\n", fs.f_fstypename);
#endif
    check("ofd_initial_acquire", a, F_OFD_SETLK, F_WRLCK, 0, 1);
    if (failed) return 1;
    check("distinct_open_same_inode_conflicts", b, F_OFD_SETLK, F_WRLCK, 0, 0);
    check("ofd_conflicts_with_same_process_posix", b, F_SETLK, F_WRLCK, 0, 0);
    child_check(argv[0], argv[1], "fresh_process_ofd_conflicts", "ofd", "busy");
    child_check(argv[0], argv[1], "fresh_process_posix_conflicts_with_ofd", "posix", "busy");
    close(c);
    check("unrelated_fd_close_preserves_ofd", b, F_OFD_SETLK, F_WRLCK, 0, 0);
    struct flock inspect = {0};
    inspect.l_type = F_WRLCK;
    inspect.l_whence = SEEK_SET;
    inspect.l_start = 0;
    inspect.l_len = 1;
    int irc = fcntl(b, F_OFD_GETLK, &inspect);
    int iok = irc == 0 && inspect.l_type == F_WRLCK;
    printf("{\"test\":\"ofd_getlk_observes_conflict\",\"rc\":%d,\"owner_pid\":%d,\"pass\":%s}\n",
           irc, inspect.l_pid, iok ? "true" : "false");
    if (!iok) failed++;
    int duplicate = fcntl(a, F_DUPFD_CLOEXEC, 0);
    if (duplicate == -1) return 2;
    close(a);
    check("closing_one_duplicate_preserves_ofd", b, F_OFD_SETLK, F_WRLCK, 0, 0);
    child_check(argv[0], argv[1], "fresh_process_after_original_fd_close", "ofd", "busy");
    close(duplicate);
    check("last_duplicate_close_releases_ofd", b, F_OFD_SETLK, F_WRLCK, 0, 1);
    check("release_b", b, F_OFD_SETLK, F_UNLCK, 0, 1);

    a = open(argv[1], O_RDWR | O_CLOEXEC);
    c = open(argv[1], O_RDWR | O_CLOEXEC);
    check("first_shared_ofd", a, F_OFD_SETLK, F_RDLCK, 0, 1);
    check("second_shared_ofd", b, F_OFD_SETLK, F_RDLCK, 0, 1);
    check("exclusive_upgrade_blocked_by_other_description", b, F_OFD_SETLK, F_WRLCK, 0, 0);
    check("release_first_shared", a, F_OFD_SETLK, F_UNLCK, 0, 1);
    check("exclusive_upgrade_after_release", b, F_OFD_SETLK, F_WRLCK, 0, 1);
    check("release_second_shared", b, F_OFD_SETLK, F_UNLCK, 0, 1);
    check("traditional_acquire", a, F_SETLK, F_WRLCK, 0, 1);
    child_check(argv[0], argv[1], "fresh_process_ofd_conflicts_with_posix", "ofd", "busy");
    child_check(argv[0], argv[1], "traditional_before_unrelated_close", "posix", "busy");
    close(c);
    child_check(argv[0], argv[1], "traditional_unrelated_close_releases_control", "posix", "success");
    close(a);
    close(b);
    printf("{\"failures\":%d}\n", failed);
    return failed ? 1 : 0;
#endif
}
