#include "localcat_native_closure.h"
#include "localcat_sha256.h"
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdlib.h>
#include <string.h>

#define SYSTEM_MEMBERS 32U
#define SYSTEM_DIRS 64U
#define SYSTEM_PATH 32768U
#define SYSTEM_NAME 128U

struct system_retained {
    HANDLE handle;
    FILE_ID_INFO identity;
    wchar_t *path;
    uint64_t size;
    unsigned char digest[32];
};
struct localcat_system_files {
    struct system_retained directories[SYSTEM_DIRS];
    struct system_retained members[SYSTEM_MEMBERS];
    size_t directory_count, member_count;
    DWORD owner_thread;
    int valid;
};

static int reject(const char **diagnostic, const char *message)
{
    if (diagnostic != NULL) { *diagnostic = message; }
    return -1;
}

static int same_identity(const FILE_ID_INFO *left, const FILE_ID_INFO *right)
{
    return left->VolumeSerialNumber == right->VolumeSerialNumber &&
        memcmp(left->FileId.Identifier, right->FileId.Identifier, 16U) == 0;
}

static int inspect(struct system_retained *entry, int directory, int first, const char **diagnostic)
{
    BY_HANDLE_FILE_INFORMATION information;
    FILE_ID_INFO identity;
    wchar_t *path;
    DWORD length;
    uint64_t size;
    if (!GetFileInformationByHandle(entry->handle, &information) ||
        !GetFileInformationByHandleEx(entry->handle, FileIdInfo, &identity, sizeof(identity)) ||
        (information.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT) != 0U ||
        !!(information.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) != directory ||
        (!directory && information.nNumberOfLinks == 0U)) {
        return reject(diagnostic, "SYSTEM_FILES.IDENTITY_OR_KIND");
    }
    path = (wchar_t *)calloc(SYSTEM_PATH, sizeof(wchar_t));
    if (path == NULL) { return reject(diagnostic, "SYSTEM_FILES.MEMORY"); }
    length = GetFinalPathNameByHandleW(entry->handle, path, SYSTEM_PATH, FILE_NAME_NORMALIZED | VOLUME_NAME_DOS);
    if (length == 0U || length >= SYSTEM_PATH ||
        (first ? CompareStringOrdinal(path, -1, entry->path, -1, TRUE) != CSTR_EQUAL : wcscmp(path, entry->path) != 0)) {
        free(path); return reject(diagnostic, "SYSTEM_FILES.FINAL_PATH");
    }
    size = ((uint64_t)information.nFileSizeHigh << 32) | information.nFileSizeLow;
    if ((!directory && size != entry->size) || (!first && !same_identity(&identity, &entry->identity))) {
        free(path); return reject(diagnostic, "SYSTEM_FILES.IDENTITY_OR_SIZE_CHANGED");
    }
    if (first) {
        entry->identity = identity;
        free(entry->path);
        entry->path = path;
    } else { free(path); }
    return 0;
}

static int open_retained(struct system_retained *entry, const wchar_t *path, int directory, const char **diagnostic)
{
    size_t length = wcslen(path);
    entry->path = (wchar_t *)calloc(length + 1U, sizeof(wchar_t));
    if (entry->path == NULL) { return reject(diagnostic, "SYSTEM_FILES.MEMORY"); }
    memcpy(entry->path, path, (length + 1U) * sizeof(wchar_t));
    /* The sharing restriction applies to the file, including OS hardlinks.
     * System servicing hardlinks are allowed here; bundle single-link is unchanged. */
    entry->handle = CreateFileW(path, FILE_READ_ATTRIBUTES | SYNCHRONIZE | (directory ? 0U : FILE_READ_DATA),
        FILE_SHARE_READ, NULL, OPEN_EXISTING, FILE_FLAG_OPEN_REPARSE_POINT |
        (directory ? FILE_FLAG_BACKUP_SEMANTICS : FILE_FLAG_SEQUENTIAL_SCAN), NULL);
    if (entry->handle == INVALID_HANDLE_VALUE) { return reject(diagnostic, "SYSTEM_FILES.OPEN_FAILED"); }
    return inspect(entry, directory, 1, diagnostic);
}

static int directories_reprove(struct localcat_system_files *files, const char **diagnostic)
{
    size_t index;
    DWORD maximum_component, volume_flags;
    wchar_t filesystem_name[64];
    if (files->directory_count == 0U) { return reject(diagnostic, "SYSTEM_FILES.VOLUME_UNSUPPORTED"); }
    for (index = 0; index < files->directory_count; ++index) {
        if (inspect(&files->directories[index], 1, 0, diagnostic) != 0 ||
            files->directories[index].identity.VolumeSerialNumber != files->directories[0].identity.VolumeSerialNumber) {
            return reject(diagnostic, "SYSTEM_FILES.DIRECTORY_CHANGED");
        }
    }
    /* Same W1 volume contract as bundle rooted I/O. FileId support alone is
     * not sufficient: remote, removable and non-NTFS volumes are excluded. */
    if (GetDriveTypeW(files->directories[0].path) != DRIVE_FIXED ||
        !GetVolumeInformationByHandleW(files->directories[0].handle, NULL, 0, NULL,
            &maximum_component, &volume_flags, filesystem_name, 64) ||
        wcscmp(filesystem_name, L"NTFS") != 0 || maximum_component < 255U ||
        !(volume_flags & FILE_PERSISTENT_ACLS) || !(volume_flags & FILE_SUPPORTS_REPARSE_POINTS)) {
        return reject(diagnostic, "SYSTEM_FILES.VOLUME_UNSUPPORTED");
    }
    return 0;
}

static int digest_reprove(struct system_retained *entry, const char **diagnostic)
{
    unsigned char buffer[16384], digest[32];
    struct localcat_sha256_context hash;
    LARGE_INTEGER start;
    DWORD count;
    uint64_t total = 0;
    start.QuadPart = 0;
    if (inspect(entry, 0, 0, diagnostic) != 0 || !SetFilePointerEx(entry->handle, start, NULL, FILE_BEGIN)) {
        return reject(diagnostic, "SYSTEM_FILES.READ_PREPROOF");
    }
    localcat_sha256_init(&hash);
    for (;;) {
        if (!ReadFile(entry->handle, buffer, sizeof(buffer), &count, NULL)) {
            return reject(diagnostic, "SYSTEM_FILES.READ_FAILED");
        }
        if (count == 0U) { break; }
        if ((uint64_t)count > entry->size - total) { return reject(diagnostic, "SYSTEM_FILES.SIZE_MISMATCH"); }
        total += count;
        localcat_sha256_update(&hash, buffer, count);
    }
    localcat_sha256_final(&hash, digest);
    if (total != entry->size || !localcat_sha256_equal(digest, entry->digest) || inspect(entry, 0, 0, diagnostic) != 0) {
        return reject(diagnostic, "SYSTEM_FILES.CONTENT_MISMATCH");
    }
    return 0;
}

static int valid_name(const wchar_t *name)
{
    size_t length, index;
    if (name == NULL) { return 0; }
    for (length = 0; length < SYSTEM_NAME && name[length] != L'\0'; ++length) { }
    if (length < 5U || length == SYSTEM_NAME || wcscmp(name + length - 4U, L".dll") != 0) { return 0; }
    for (index = 0; index < length - 4U; ++index) {
        wchar_t c = name[index];
        if (!((c >= L'a' && c <= L'z') || (c >= L'0' && c <= L'9') || c == L'_' || c == L'-')) { return 0; }
    }
    return 1;
}

void localcat_system_files_close(struct localcat_system_files **pointer)
{
    struct localcat_system_files *files;
    size_t index;
    if (pointer == NULL || *pointer == NULL) { return; }
    files = *pointer;
    if (files->owner_thread != GetCurrentThreadId()) { return; }
    *pointer = NULL;
    for (index = 0; index < files->member_count; ++index) {
        if (files->members[index].handle != NULL && files->members[index].handle != INVALID_HANDLE_VALUE) { CloseHandle(files->members[index].handle); }
        free(files->members[index].path);
    }
    for (index = files->directory_count; index > 0U; --index) {
        struct system_retained *entry = &files->directories[index - 1U];
        if (entry->handle != NULL && entry->handle != INVALID_HANDLE_VALUE) { CloseHandle(entry->handle); }
        free(entry->path);
    }
    free(files);
}

int localcat_system_files_reprove(struct localcat_system_files *files, const char **diagnostic)
{
    size_t index;
    if (diagnostic != NULL) { *diagnostic = NULL; }
    if (files == NULL || !files->valid || files->owner_thread != GetCurrentThreadId()) {
        return reject(diagnostic, "SYSTEM_FILES.UNAVAILABLE");
    }
    if (directories_reprove(files, diagnostic) != 0) { files->valid = 0; return -1; }
    for (index = 0; index < files->member_count; ++index) {
        if (digest_reprove(&files->members[index], diagnostic) != 0) { files->valid = 0; return -1; }
    }
    if (directories_reprove(files, diagnostic) != 0) { files->valid = 0; return -1; }
    return 0;
}

int localcat_system_files_prepare(const struct localcat_system_file_candidate *candidates,
    size_t count, struct localcat_system_files **output, const char **diagnostic)
{
    struct localcat_system_files *files = NULL;
    wchar_t *system = NULL, *path = NULL;
    UINT length;
    size_t index, other, end, base_length;
    if (diagnostic != NULL) { *diagnostic = NULL; }
    if (candidates == NULL || output == NULL || *output != NULL || count == 0U || count > SYSTEM_MEMBERS) {
        return reject(diagnostic, "SYSTEM_FILES.ARGUMENT");
    }
    for (index = 0; index < count; ++index) {
        if (!valid_name(candidates[index].basename) || candidates[index].size == 0U) {
            return reject(diagnostic, "SYSTEM_FILES.CANDIDATE");
        }
        for (other = 0; other < index; ++other) {
            if (wcscmp(candidates[index].basename, candidates[other].basename) == 0) { return reject(diagnostic, "SYSTEM_FILES.DUPLICATE"); }
        }
    }
    files = (struct localcat_system_files *)calloc(1U, sizeof(*files));
    system = (wchar_t *)calloc(SYSTEM_PATH, sizeof(wchar_t));
    path = (wchar_t *)calloc(SYSTEM_PATH, sizeof(wchar_t));
    if (files == NULL || system == NULL || path == NULL) { reject(diagnostic, "SYSTEM_FILES.MEMORY"); goto failed; }
    files->owner_thread = GetCurrentThreadId();
    length = GetSystemDirectoryW(system, SYSTEM_PATH);
    if (length < 4U || length >= SYSTEM_PATH - SYSTEM_NAME - 6U ||
        !((system[0] >= L'A' && system[0] <= L'Z') || (system[0] >= L'a' && system[0] <= L'z')) ||
        system[1] != L':' || system[2] != L'\\' || system[length - 1U] == L'\\') {
        reject(diagnostic, "SYSTEM_FILES.SYSTEM_ROOT"); goto failed;
    }
    memcpy(path, L"\\\\?\\", 4U * sizeof(wchar_t));
    memcpy(path + 4U, system, 3U * sizeof(wchar_t));
    files->directory_count = 1U;
    if (open_retained(&files->directories[0], path, 1, diagnostic) != 0 ||
        directories_reprove(files, diagnostic) != 0) { goto failed; }
    for (end = 3U; end <= length; ++end) {
        if (system[end] != L'\\' && end != length) { continue; }
        if (end == 3U || system[end - 1U] == L'\\' || files->directory_count >= SYSTEM_DIRS) {
            reject(diagnostic, "SYSTEM_FILES.SYSTEM_ROOT"); goto failed;
        }
        memcpy(path + 4U, system, end * sizeof(wchar_t)); path[end + 4U] = L'\0';
        if (directories_reprove(files, diagnostic) != 0) { goto failed; }
        ++files->directory_count;
        if (open_retained(&files->directories[files->directory_count - 1U], path, 1, diagnostic) != 0 ||
            directories_reprove(files, diagnostic) != 0) { goto failed; }
    }
    base_length = wcslen(files->directories[files->directory_count - 1U].path);
    for (index = 0; index < count; ++index) {
        struct system_retained *entry = &files->members[index];
        if (directories_reprove(files, diagnostic) != 0) { goto failed; }
        memcpy(path, files->directories[files->directory_count - 1U].path, base_length * sizeof(wchar_t));
        path[base_length] = L'\\';
        memcpy(path + base_length + 1U, candidates[index].basename, (wcslen(candidates[index].basename) + 1U) * sizeof(wchar_t));
        entry->size = candidates[index].size;
        memcpy(entry->digest, candidates[index].sha256, 32U);
        ++files->member_count;
        if (open_retained(entry, path, 0, diagnostic) != 0 ||
            entry->identity.VolumeSerialNumber != files->directories[0].identity.VolumeSerialNumber) {
            if (diagnostic != NULL && *diagnostic == NULL) { *diagnostic = "SYSTEM_FILES.VOLUME_MISMATCH"; }
            goto failed;
        }
    }
    files->valid = 1;
    if (localcat_system_files_reprove(files, diagnostic) != 0) { goto failed; }
    free(system); free(path); *output = files;
    return 0;
failed:
    free(system); free(path);
    if (files != NULL) { files->owner_thread = GetCurrentThreadId(); localcat_system_files_close(&files); }
    return -1;
}
