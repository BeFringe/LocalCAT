#ifdef _WIN32

#include "localcat_rooted_io.h"
#include "localcat_sha256.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

#ifndef FILE_ATTRIBUTE_TAG_INFO
#define FILE_ATTRIBUTE_TAG_INFO 9
#endif

struct localcat_file_attribute_tag_info {
    DWORD file_attributes;
    DWORD reparse_tag;
};

static int localcat_native_preload_barrier(struct localcat_bundle_authority *authority, const char **diagnostic);
static int localcat_verify_bundle_inventory(struct localcat_bundle_authority *authority, const char **diagnostic);

static int
localcat_identity_for_handle(HANDLE handle, struct localcat_file_identity *identity)
{
    FILE_ID_INFO information;
    if (!GetFileInformationByHandleEx(handle, FileIdInfo, &information, sizeof(information))) {
        return -1;
    }
    identity->volume_serial = information.VolumeSerialNumber;
    memcpy(identity->file_id, information.FileId.Identifier, sizeof(identity->file_id));
    return 0;
}

static int
localcat_identity_equal(const struct localcat_file_identity *left, const struct localcat_file_identity *right)
{
    return left->volume_serial == right->volume_serial && memcmp(left->file_id, right->file_id, 16) == 0;
}

static int
localcat_handle_is_reparse(HANDLE handle)
{
    struct localcat_file_attribute_tag_info information;
    if (!GetFileInformationByHandleEx(handle, (FILE_INFO_BY_HANDLE_CLASS)FILE_ATTRIBUTE_TAG_INFO, &information, sizeof(information))) {
        return -1;
    }
    return (information.file_attributes & FILE_ATTRIBUTE_REPARSE_POINT) != 0 ? 1 : 0;
}

static int
localcat_final_path(HANDLE handle, wchar_t output[32768])
{
    DWORD length = GetFinalPathNameByHandleW(handle, output, 32768, FILE_NAME_NORMALIZED | VOLUME_NAME_DOS);
    if (length == 0 || length >= 32768) {
        return -1;
    }
    return 0;
}

static int
localcat_open_path(
    const wchar_t *path,
    int directory,
    HANDLE *output,
    struct localcat_file_identity *identity,
    wchar_t final_path[32768],
    const char **diagnostic
)
{
    DWORD flags = FILE_FLAG_OPEN_REPARSE_POINT | (directory ? FILE_FLAG_BACKUP_SEMANTICS : FILE_FLAG_SEQUENTIAL_SCAN);
    HANDLE handle = CreateFileW(
        path,
        FILE_READ_DATA | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
        FILE_SHARE_READ,
        NULL,
        OPEN_EXISTING,
        flags,
        NULL
    );
    int reparse;
    BY_HANDLE_FILE_INFORMATION basic;
    if (handle == INVALID_HANDLE_VALUE) {
        *diagnostic = "FROZEN_ENTRY.ROOTED_OPEN_FAILED";
        return -1;
    }
    reparse = localcat_handle_is_reparse(handle);
    if (reparse != 0) {
        *diagnostic = reparse > 0 ? "FROZEN_ENTRY.REPARSE_REJECTED" : "FROZEN_ENTRY.IDENTITY_QUERY_FAILED";
        CloseHandle(handle);
        return -1;
    }
    if (!GetFileInformationByHandle(handle, &basic)) {
        *diagnostic = "FROZEN_ENTRY.IDENTITY_QUERY_FAILED";
        CloseHandle(handle);
        return -1;
    }
    if (directory ? !(basic.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) : (basic.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)) {
        *diagnostic = "FROZEN_ENTRY.WRONG_KIND";
        CloseHandle(handle);
        return -1;
    }
    if (basic.nNumberOfLinks != 1U && !directory) {
        *diagnostic = "FROZEN_ENTRY.MULTI_LINK_REJECTED";
        CloseHandle(handle);
        return -1;
    }
    if (localcat_identity_for_handle(handle, identity) != 0 || localcat_final_path(handle, final_path) != 0) {
        *diagnostic = "FROZEN_ENTRY.IDENTITY_QUERY_FAILED";
        CloseHandle(handle);
        return -1;
    }
    *output = handle;
    return 0;
}

static int
localcat_append_component(wchar_t *path, size_t capacity, const wchar_t *component, size_t length)
{
    size_t current = wcslen(path);
    if (current != 0 && path[current - 1] != L'\\') {
        if (current + 1 >= capacity) { return -1; }
        path[current++] = L'\\';
        path[current] = 0;
    }
    if (length >= capacity - current) { return -1; }
    memcpy(path + current, component, length * sizeof(wchar_t));
    path[current + length] = 0;
    return 0;
}

static int
localcat_compare_exact_path(const wchar_t *expected, const wchar_t *actual)
{
    return wcscmp(expected, actual) == 0 ? 0 : -1;
}

static int
localcat_reprove_directories(struct localcat_bundle_authority *authority, const char **diagnostic)
{
    unsigned int index;
    wchar_t final_path[32768];
    struct localcat_file_identity identity;
    if (authority == NULL || authority->state == 3U || authority->directory_count == 0U) {
        *diagnostic = "FROZEN_ENTRY.PARENT_STALE";
        return -1;
    }
    for (index = 0; index < authority->directory_count; ++index) {
        HANDLE handle = authority->directory_handles[index];
        if (handle == NULL || localcat_handle_is_reparse(handle) != 0 ||
            localcat_identity_for_handle(handle, &identity) != 0 ||
            !localcat_identity_equal(&identity, &authority->directory_identities[index]) ||
            localcat_final_path(handle, final_path) != 0 ||
            localcat_compare_exact_path(final_path, authority->directory_final_paths[index]) != 0) {
            *diagnostic = "FROZEN_ENTRY.PARENT_STALE";
            return -1;
        }
    }
    return 0;
}

static int
localcat_retain_directory(struct localcat_bundle_authority *authority, const wchar_t *path, const char **diagnostic)
{
    HANDLE handle;
    struct localcat_file_identity identity;
    wchar_t final_path[32768];
    wchar_t *saved;
    unsigned int index;
    if (authority->directory_count != 0U && localcat_reprove_directories(authority, diagnostic) != 0) { return -1; }
    for (index = 0; index < authority->directory_count; ++index) {
        if (localcat_compare_exact_path(path, authority->directory_final_paths[index]) == 0) { return 0; }
    }
    if (authority->directory_count >= LOCALCAT_MAX_RETAINED_DIRECTORIES) {
        *diagnostic = "FROZEN_ENTRY.DIRECTORY_LIMIT";
        return -1;
    }
    if (localcat_open_path(path, 1, &handle, &identity, final_path, diagnostic) != 0) { return -1; }
    if (localcat_compare_exact_path(path, final_path) != 0 ||
        (authority->directory_count != 0U && identity.volume_serial != authority->directory_identities[0].volume_serial)) {
        *diagnostic = "FROZEN_ENTRY.BUNDLE_ALIAS_REJECTED";
        CloseHandle(handle);
        return -1;
    }
    saved = (wchar_t *)malloc((wcslen(final_path) + 1U) * sizeof(wchar_t));
    if (saved == NULL) {
        *diagnostic = "FROZEN_ENTRY.ALLOCATION_FAILED";
        CloseHandle(handle);
        return -1;
    }
    wcscpy_s(saved, wcslen(final_path) + 1U, final_path);
    index = authority->directory_count++;
    authority->directory_handles[index] = handle;
    authority->directory_identities[index] = identity;
    authority->directory_final_paths[index] = saved;
    return 0;
}

static int
localcat_bind_bundle_root(struct localcat_bundle_authority *authority, const char **diagnostic)
{
    wchar_t executable_path[32768];
    wchar_t volume_root[8];
    wchar_t cumulative[32768];
    wchar_t expected_extended[32768];
    wchar_t *bundle_end;
    const wchar_t *cursor;
    DWORD executable_length;
    size_t volume_length;
    wchar_t filesystem_name[64];
    DWORD volume_flags, maximum_component;

    executable_length = GetModuleFileNameW(NULL, executable_path, 32768);
    if (executable_length == 0 || executable_length >= 32768) {
        *diagnostic = "FROZEN_ENTRY.EXECUTABLE_PATH_FAILED";
        return -1;
    }
    if (wcsncmp(executable_path, L"\\\\?\\", 4) == 0) {
        wcscpy_s(authority->executable_final_path, 32768, executable_path);
    } else if (swprintf_s(authority->executable_final_path, 32768, L"\\\\?\\%ls", executable_path) < 0) {
        *diagnostic = "FROZEN_ENTRY.EXECUTABLE_PATH_FAILED";
        return -1;
    }
    bundle_end = wcsrchr(executable_path, L'\\');
    if (bundle_end == NULL) {
        *diagnostic = "FROZEN_ENTRY.EXECUTABLE_PATH_FAILED";
        return -1;
    }
    if (bundle_end == executable_path + 2 ||
        (wcsncmp(executable_path, L"\\\\?\\", 4) == 0 && bundle_end == executable_path + 6)) {
        bundle_end[1] = 0;
    } else { *bundle_end = 0; }
    /* Only ordinary drive-letter local paths are accepted. Do not follow a
     * mount point first and then accidentally omit its ancestors. */
    if (wcsncmp(executable_path, L"\\\\?\\", 4) == 0) {
        memmove(executable_path, executable_path + 4, (wcslen(executable_path + 4) + 1U) * sizeof(wchar_t));
    }
    if (!((executable_path[0] >= L'A' && executable_path[0] <= L'Z') ||
        (executable_path[0] >= L'a' && executable_path[0] <= L'z')) || executable_path[1] != L':' || executable_path[2] != L'\\') {
        *diagnostic = "FROZEN_ENTRY.VOLUME_UNSUPPORTED";
        return -1;
    }
    swprintf_s(volume_root, 8, L"\\\\?\\%lc:\\", executable_path[0]);
    if (GetDriveTypeW(volume_root) != DRIVE_FIXED || localcat_retain_directory(authority, volume_root, diagnostic) != 0) {
        return -1;
    }
    if (!GetVolumeInformationByHandleW(authority->directory_handles[0], NULL, 0, NULL, &maximum_component, &volume_flags, filesystem_name, 64) ||
        wcscmp(filesystem_name, L"NTFS") != 0 || maximum_component < 255U ||
        !(volume_flags & FILE_PERSISTENT_ACLS) || !(volume_flags & FILE_SUPPORTS_REPARSE_POINTS)) {
        *diagnostic = "FROZEN_ENTRY.VOLUME_UNSUPPORTED";
        return -1;
    }
    wcscpy_s(cumulative, 32768, volume_root);
    volume_length = 3U;
    cursor = executable_path + volume_length;
    while (*cursor != 0) {
        const wchar_t *separator;
        size_t component_length;
        while (*cursor == L'\\') { ++cursor; }
        if (*cursor == 0) { break; }
        separator = wcschr(cursor, L'\\');
        component_length = separator ? (size_t)(separator - cursor) : wcslen(cursor);
        if (authority->directory_count >= LOCALCAT_MAX_RETAINED_DIRECTORIES ||
            localcat_append_component(cumulative, 32768, cursor, component_length) != 0 ||
            localcat_retain_directory(authority, cumulative, diagnostic) != 0) {
            return -1;
        }
        cursor += component_length;
    }
    if (swprintf_s(expected_extended, 32768, L"\\\\?\\%ls", executable_path) < 0 ||
        localcat_compare_exact_path(expected_extended, cumulative) != 0) {
        *diagnostic = "FROZEN_ENTRY.BUNDLE_ALIAS_REJECTED";
        return -1;
    }
    wcscpy_s(authority->root_final_path, 32768, cumulative);
    return 0;
}

static int
localcat_utf8_path_to_wide(const unsigned char *path, uint32_t length, wchar_t *output, size_t capacity)
{
    int converted;
    if (length > INT_MAX) { return -1; }
    converted = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, (const char *)path, (int)length, output, (int)(capacity - 1));
    if (converted <= 0 || (size_t)converted >= capacity) { return -1; }
    output[converted] = 0;
    return 0;
}

static int
localcat_open_bound_regular(
    struct localcat_bundle_authority *authority, const wchar_t *expected,
    HANDLE *handle, struct localcat_file_identity *identity, wchar_t final_path[32768], const char **diagnostic
)
{
    wchar_t parent[32768];
    wchar_t *separator;
    unsigned int index;
    int parent_found = 0;
    if (localcat_reprove_directories(authority, diagnostic) != 0) { return -1; }
    wcscpy_s(parent, 32768, expected);
    separator = wcsrchr(parent, L'\\');
    if (separator == NULL) { return -1; }
    if (separator == parent + 6) { separator[1] = 0; } else { *separator = 0; }
    for (index = 0; index < authority->directory_count; ++index) {
        if (localcat_compare_exact_path(parent, authority->directory_final_paths[index]) == 0) { parent_found = 1; break; }
    }
    if (!parent_found) { *diagnostic = "FROZEN_ENTRY.PARENT_UNBOUND"; return -1; }
    if (localcat_open_path(expected, 0, handle, identity, final_path, diagnostic) != 0) { return -1; }
    if (localcat_compare_exact_path(expected, final_path) != 0 ||
        identity->volume_serial != authority->directory_identities[0].volume_serial ||
        localcat_reprove_directories(authority, diagnostic) != 0) {
        *diagnostic = "FROZEN_ENTRY.ENTRY_ALIAS_REJECTED";
        CloseHandle(*handle);
        *handle = NULL;
        return -1;
    }
    return 0;
}

static int
localcat_bind_executable_file(struct localcat_bundle_authority *authority, const char **diagnostic)
{
    wchar_t final_path[32768];
    return localcat_open_bound_regular(authority, authority->executable_final_path,
        &authority->executable_handle, &authority->executable_identity, final_path, diagnostic);
}

static int
localcat_open_manifest(struct localcat_bundle_authority *authority, const char **diagnostic)
{
    wchar_t manifest_path[32768];
    HANDLE handle;
    struct localcat_file_identity identity;
    wchar_t final_path[32768];
    LARGE_INTEGER size;
    DWORD read_count;
    unsigned char digest[32];
    struct localcat_sha256_context sha;
    unsigned char extra;

    if (swprintf_s(manifest_path, 32768, L"%ls\\localcat-runtime.manifest", authority->root_final_path) < 0 ||
        localcat_open_bound_regular(authority, manifest_path, &handle, &identity, final_path, diagnostic) != 0) {
        return -1;
    }
    if (!GetFileSizeEx(handle, &size) || size.QuadPart <= 0 || size.QuadPart > 1024 * 1024) {
        *diagnostic = "FROZEN_ENTRY.MANIFEST_SIZE_INVALID";
        CloseHandle(handle);
        return -1;
    }
    authority->manifest_bytes = (unsigned char *)malloc((size_t)size.QuadPart);
    if (authority->manifest_bytes == NULL) {
        *diagnostic = "FROZEN_ENTRY.ALLOCATION_FAILED";
        CloseHandle(handle);
        return -1;
    }
    if (!ReadFile(handle, authority->manifest_bytes, (DWORD)size.QuadPart, &read_count, NULL) || read_count != (DWORD)size.QuadPart ||
        !ReadFile(handle, &extra, 1, &read_count, NULL) || read_count != 0U) {
        *diagnostic = "FROZEN_ENTRY.MANIFEST_READ_FAILED";
        CloseHandle(handle);
        return -1;
    }
    localcat_sha256_init(&sha);
    localcat_sha256_update(&sha, authority->manifest_bytes, (size_t)size.QuadPart);
    localcat_sha256_final(&sha, digest);
    if (!localcat_sha256_equal(digest, localcat_embedded_runtime_manifest_digest)) {
        *diagnostic = "FROZEN_ENTRY.MANIFEST_DIGEST_MISMATCH";
        CloseHandle(handle);
        return -1;
    }
    if (localcat_manifest_parse(&authority->manifest, authority->manifest_bytes, (size_t)size.QuadPart, diagnostic) != 0) {
        CloseHandle(handle);
        return -1;
    }
    authority->manifest_handle = handle;
    authority->manifest_identity = identity;
    wcscpy_s(authority->manifest_final_path, 32768, final_path);
    return 0;
}

static int
localcat_open_manifest_entries(struct localcat_bundle_authority *authority, const char **diagnostic)
{
    uint32_t index;
    wchar_t relative[2048];
    wchar_t path[32768];
    for (index = 0; index < authority->manifest.entry_count; ++index) {
        const struct localcat_manifest_entry *manifest_entry = &authority->manifest.entries[index];
        struct localcat_retained_entry *entry = &authority->entries[index];
        HANDLE handle;
        struct localcat_file_identity identity;
        wchar_t final_path[32768];
        const wchar_t *cursor;
        const wchar_t *separator;
        unsigned char *verified_bytes;
        size_t verified_size;
        if (localcat_utf8_path_to_wide(manifest_entry->path, manifest_entry->path_length, relative, 2048) != 0) {
            *diagnostic = "FROZEN_ENTRY.MANIFEST_PATH_INVALID";
            return -1;
        }
        wcscpy_s(path, 32768, authority->root_final_path);
        cursor = relative;
        while ((separator = wcschr(cursor, L'\\')) != NULL) {
            if (localcat_append_component(path, 32768, cursor, (size_t)(separator - cursor)) != 0 ||
                localcat_retain_directory(authority, path, diagnostic) != 0) { return -1; }
            cursor = separator + 1;
        }
        if (localcat_append_component(path, 32768, cursor, wcslen(cursor)) != 0 ||
            localcat_open_bound_regular(authority, path, &handle, &identity, final_path, diagnostic) != 0) {
            return -1;
        }
        entry->authority = authority;
        entry->manifest_entry = manifest_entry;
        entry->handle = handle;
        entry->identity = identity;
        /* Transfer cleanup ownership before any fallible content proof. */
        authority->entry_count++;
        entry->final_path = (wchar_t *)malloc((wcslen(final_path) + 1U) * sizeof(wchar_t));
        if (entry->final_path == NULL) {
            *diagnostic = "FROZEN_ENTRY.ALLOCATION_FAILED";
            return -1;
        }
        wcscpy_s(entry->final_path, wcslen(final_path) + 1U, final_path);
        if (localcat_retained_entry_read_verified(entry, &verified_bytes, &verified_size, diagnostic) != 0) {
            return -1;
        }
        free(verified_bytes);
        entry->digest_proved = 1U;
    }
    authority->all_entries_proved = 1U;
    return 0;
}

static int
localcat_path_below(const wchar_t *path, const wchar_t *parent)
{
    size_t length = wcslen(parent);
    return wcslen(path) > length && wcsncmp(path, parent, length) == 0 &&
        (parent[length - 1U] == L'\\' || path[length] == L'\\');
}

static int
localcat_reprove_control_file(HANDLE handle, const wchar_t *expected,
    const struct localcat_file_identity *expected_identity, const char **diagnostic)
{
    struct localcat_file_identity identity;
    wchar_t final_path[32768];
    BY_HANDLE_FILE_INFORMATION basic;
    if (handle == NULL || handle == INVALID_HANDLE_VALUE || localcat_handle_is_reparse(handle) != 0 ||
        !GetFileInformationByHandle(handle, &basic) || basic.nNumberOfLinks != 1U ||
        (basic.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) ||
        localcat_identity_for_handle(handle, &identity) != 0 || !localcat_identity_equal(&identity, expected_identity) ||
        localcat_final_path(handle, final_path) != 0 || localcat_compare_exact_path(expected, final_path) != 0) {
        *diagnostic = "FROZEN_ENTRY.INVENTORY_CONTROL_STALE";
        return -1;
    }
    return 0;
}

static int
localcat_inventory_pass(struct localcat_bundle_authority *authority, const char **diagnostic)
{
    unsigned int queue[LOCALCAT_MAX_RETAINED_DIRECTORIES];
    unsigned char seen_directories[LOCALCAT_MAX_RETAINED_DIRECTORIES] = {0};
    unsigned char seen_entries[LOCALCAT_MANIFEST_MAX_ENTRIES] = {0};
    unsigned int queued = 0, next = 0, index;
    unsigned int seen_executable = 0, seen_manifest = 0;
    wchar_t path[32768];
    WIN32_FIND_DATAW found;
    HANDLE search;
    DWORD enumeration_error;
    if (localcat_reprove_directories(authority, diagnostic) != 0) { return -1; }
    for (index = 0; index < authority->directory_count; ++index) {
        if (localcat_compare_exact_path(authority->directory_final_paths[index], authority->root_final_path) == 0) {
            queue[queued++] = index; seen_directories[index] = 1U; break;
        }
    }
    if (queued != 1U) { *diagnostic = "FROZEN_ENTRY.PARENT_UNBOUND"; return -1; }
    /* Iterative breadth-first traversal keeps native stack use bounded even
     * for deeply nested allowed paths. Only manifest-required directories can
     * enter the queue; each was already opened no-follow and retained. */
    while (next < queued) {
        const wchar_t *parent = authority->directory_final_paths[queue[next++]];
        if (localcat_reprove_directories(authority, diagnostic) != 0) { return -1; }
        wcscpy_s(path, 32768, parent);
        if (localcat_append_component(path, 32768, L"*", 1U) != 0) { return -1; }
        search = FindFirstFileW(path, &found);
        if (search == INVALID_HANDLE_VALUE) { *diagnostic = "FROZEN_ENTRY.INVENTORY_ENUMERATION_FAILED"; return -1; }
        do {
            const wchar_t *extension;
            int matched = 0;
            if (wcscmp(found.cFileName, L".") == 0 || wcscmp(found.cFileName, L"..") == 0) { continue; }
            *diagnostic = "FROZEN_ENTRY.INVENTORY_EXTRA";
            if (found.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT) {
                *diagnostic = "FROZEN_ENTRY.REPARSE_REJECTED"; FindClose(search); return -1;
            }
            extension = wcsrchr(found.cFileName, L'.');
            if (_wcsicmp(found.cFileName, L"__pycache__") == 0 ||
                (extension != NULL && (_wcsicmp(extension, L".pyc") == 0 || _wcsicmp(extension, L".pyz") == 0))) {
                *diagnostic = "FROZEN_ENTRY.SOURCE_DUPLICATE"; FindClose(search); return -1;
            }
            wcscpy_s(path, 32768, parent);
            if (localcat_append_component(path, 32768, found.cFileName, wcslen(found.cFileName)) != 0) {
                FindClose(search); return -1;
            }
            if (found.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) {
                for (index = 0; index < authority->directory_count; ++index) {
                    if (localcat_compare_exact_path(path, authority->directory_final_paths[index]) == 0) {
                        unsigned int member;
                        int necessary = 0;
                        for (member = 0; member < authority->entry_count; ++member) {
                            if (localcat_path_below(authority->entries[member].final_path, path)) { necessary = 1; break; }
                        }
                        if (!necessary || seen_directories[index] || queued >= LOCALCAT_MAX_RETAINED_DIRECTORIES ||
                            localcat_retain_directory(authority, path, diagnostic) != 0) {
                            FindClose(search); return -1;
                        }
                        seen_directories[index] = 1U; queue[queued++] = index; matched = 1; break;
                    }
                }
            } else if (localcat_compare_exact_path(path, authority->executable_final_path) == 0) {
                matched = ++seen_executable == 1U && localcat_reprove_control_file(authority->executable_handle,
                    authority->executable_final_path, &authority->executable_identity, diagnostic) == 0;
            } else if (localcat_compare_exact_path(path, authority->manifest_final_path) == 0) {
                matched = ++seen_manifest == 1U && localcat_reprove_control_file(authority->manifest_handle,
                    authority->manifest_final_path, &authority->manifest_identity, diagnostic) == 0;
            } else {
                for (index = 0; index < authority->entry_count; ++index) {
                    if (localcat_compare_exact_path(path, authority->entries[index].final_path) == 0) {
                        matched = !seen_entries[index] && localcat_retained_entry_reprove(&authority->entries[index], diagnostic) == 0;
                        seen_entries[index] = 1U; break;
                    }
                }
            }
            if (!matched) { FindClose(search); return -1; }
        } while (FindNextFileW(search, &found));
        enumeration_error = GetLastError();
        FindClose(search);
        if (enumeration_error != ERROR_NO_MORE_FILES) {
            *diagnostic = "FROZEN_ENTRY.INVENTORY_ENUMERATION_FAILED"; return -1;
        }
    }
    *diagnostic = "FROZEN_ENTRY.INVENTORY_MISSING";
    if (seen_executable != 1U || seen_manifest != 1U) { return -1; }
    for (index = 0; index < authority->entry_count; ++index) { if (!seen_entries[index]) { return -1; } }
    for (index = 0; index < authority->directory_count; ++index) {
        if (localcat_path_below(authority->directory_final_paths[index], authority->root_final_path) && !seen_directories[index]) { return -1; }
    }
    return localcat_reprove_directories(authority, diagnostic);
}

static int
localcat_verify_bundle_inventory(struct localcat_bundle_authority *authority, const char **diagnostic)
{
    /* Reject extras observed in either pass, not additions after enumeration.
     * Retained directory handles do not pin child-name absence: this cannot
     * authorize a pathname-based Python/bytecode fallback. Appended CArchive
     * duplicate proof remains a separate build gate. */
    if (localcat_inventory_pass(authority, diagnostic) != 0) { return -1; }
    return localcat_inventory_pass(authority, diagnostic);
}

int
localcat_bundle_authority_recheck_inventory(struct localcat_bundle_authority *authority, const char **diagnostic)
{
    if (authority == NULL || (authority->state != 1U && authority->state != 2U) ||
        !authority->all_entries_proved) {
        *diagnostic = "FROZEN_ENTRY.INVENTORY_STATE"; return -1;
    }
    return localcat_verify_bundle_inventory(authority, diagnostic);
}

int
localcat_bundle_authority_prepare(struct localcat_bundle_authority *authority, const char **diagnostic)
{
    memset(authority, 0, sizeof(*authority));
    *diagnostic = "FROZEN_ENTRY.PREPARE_FAILED";
    if (localcat_bind_bundle_root(authority, diagnostic) != 0 ||
        localcat_bind_executable_file(authority, diagnostic) != 0 ||
        localcat_open_manifest(authority, diagnostic) != 0 ||
        localcat_open_manifest_entries(authority, diagnostic) != 0 ||
        localcat_verify_bundle_inventory(authority, diagnostic) != 0 ||
        localcat_native_preload_barrier(authority, diagnostic) != 0) {
        localcat_bundle_authority_close(authority);
        return -1;
    }
    authority->state = 1U;
    return 0;
}

void
localcat_bundle_authority_close(struct localcat_bundle_authority *authority)
{
    unsigned int index;
    authority->all_entries_proved = 0U;
    for (index = authority->entry_count; index-- > 0U;) {
        if (authority->entries[index].handle != NULL && authority->entries[index].handle != INVALID_HANDLE_VALUE) {
            CloseHandle(authority->entries[index].handle);
            authority->entries[index].handle = NULL;
        }
        authority->entries[index].digest_proved = 0U;
        authority->entries[index].actual_module_reproved = 0U;
        free(authority->entries[index].final_path);
        authority->entries[index].final_path = NULL;
    }
    if (authority->manifest_handle != NULL) { CloseHandle(authority->manifest_handle); authority->manifest_handle = NULL; }
    if (authority->executable_handle != NULL) { CloseHandle(authority->executable_handle); authority->executable_handle = NULL; }
    for (index = authority->directory_count; index-- > 0U;) {
        if (authority->directory_handles[index] != NULL && authority->directory_handles[index] != INVALID_HANDLE_VALUE) {
            CloseHandle(authority->directory_handles[index]);
            authority->directory_handles[index] = NULL;
        }
        free(authority->directory_final_paths[index]);
        authority->directory_final_paths[index] = NULL;
    }
    free(authority->manifest_bytes);
    authority->manifest_bytes = NULL;
    authority->entry_count = 0;
    authority->directory_count = 0;
    authority->state = 3U;
}

struct localcat_retained_entry *
localcat_bundle_authority_find(struct localcat_bundle_authority *authority, const char *id)
{
    unsigned int index;
    for (index = 0; index < authority->entry_count; ++index) {
        const struct localcat_manifest_entry *manifest_entry = authority->entries[index].manifest_entry;
        size_t id_size = strlen(id);
        if (id_size == manifest_entry->id_length && memcmp(manifest_entry->id, id, id_size) == 0) {
            return &authority->entries[index];
        }
    }
    return NULL;
}

int
localcat_retained_entry_read_verified(
    struct localcat_retained_entry *entry,
    unsigned char **bytes,
    size_t *byte_count,
    const char **diagnostic
)
{
    LARGE_INTEGER before_size, after_size, zero;
    struct localcat_file_identity before_identity, after_identity;
    unsigned char *buffer;
    DWORD read_count, extra_count;
    unsigned char extra;
    struct localcat_sha256_context sha;
    unsigned char digest[32];
    zero.QuadPart = 0;
    *bytes = NULL;
    *byte_count = 0;
    if (entry != NULL && entry->authority != NULL && (entry->authority->state == 1U || entry->authority->state == 2U) &&
        localcat_verify_bundle_inventory(entry->authority, diagnostic) != 0) { return -1; }
    if (entry == NULL || entry->handle == NULL || localcat_retained_entry_reprove(entry, diagnostic) != 0 ||
        localcat_identity_for_handle(entry->handle, &before_identity) != 0 ||
        !localcat_identity_equal(&before_identity, &entry->identity) ||
        !GetFileSizeEx(entry->handle, &before_size) || before_size.QuadPart < 0 ||
        (uint64_t)before_size.QuadPart != entry->manifest_entry->byte_count || before_size.QuadPart > 32 * 1024 * 1024 ||
        !SetFilePointerEx(entry->handle, zero, NULL, FILE_BEGIN)) {
        *diagnostic = "FROZEN_ENTRY.ENTRY_STALE";
        return -1;
    }
    buffer = (unsigned char *)malloc((size_t)before_size.QuadPart + 1U);
    if (buffer == NULL) {
        *diagnostic = "FROZEN_ENTRY.ALLOCATION_FAILED";
        return -1;
    }
    if (!ReadFile(entry->handle, buffer, (DWORD)before_size.QuadPart, &read_count, NULL) ||
        read_count != (DWORD)before_size.QuadPart || !ReadFile(entry->handle, &extra, 1, &extra_count, NULL) || extra_count != 0U ||
        !GetFileSizeEx(entry->handle, &after_size) || after_size.QuadPart != before_size.QuadPart ||
        localcat_identity_for_handle(entry->handle, &after_identity) != 0 || !localcat_identity_equal(&before_identity, &after_identity)) {
        free(buffer);
        *diagnostic = "FROZEN_ENTRY.ENTRY_READ_CHANGED";
        return -1;
    }
    localcat_sha256_init(&sha);
    localcat_sha256_update(&sha, buffer, (size_t)before_size.QuadPart);
    localcat_sha256_final(&sha, digest);
    if (!localcat_sha256_equal(digest, entry->manifest_entry->digest)) {
        free(buffer);
        *diagnostic = "FROZEN_ENTRY.ENTRY_DIGEST_MISMATCH";
        return -1;
    }
    if ((entry->manifest_entry->role == LOCALCAT_ROLE_BOOTSTRAP ||
         entry->manifest_entry->role == LOCALCAT_ROLE_CRITICAL_SOURCE ||
         entry->manifest_entry->role == LOCALCAT_ROLE_INTERPRETER) &&
        memchr(buffer, 0, (size_t)before_size.QuadPart) != NULL) {
        free(buffer);
        *diagnostic = "FROZEN_ENTRY.SOURCE_NUL_REJECTED";
        return -1;
    }
    buffer[(size_t)before_size.QuadPart] = 0;
    if (localcat_retained_entry_reprove(entry, diagnostic) != 0) { free(buffer); return -1; }
    *bytes = buffer;
    *byte_count = (size_t)before_size.QuadPart;
    return 0;
}

int
localcat_retained_entry_reprove(struct localcat_retained_entry *entry, const char **diagnostic)
{
    struct localcat_file_identity identity;
    wchar_t final_path[32768];
    BY_HANDLE_FILE_INFORMATION basic;
    if (entry == NULL || entry->handle == NULL || entry->manifest_entry == NULL ||
        localcat_reprove_directories(entry->authority, diagnostic) != 0 ||
        localcat_handle_is_reparse(entry->handle) != 0 || localcat_identity_for_handle(entry->handle, &identity) != 0 ||
        !GetFileInformationByHandle(entry->handle, &basic) || basic.nNumberOfLinks != 1U ||
        (basic.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) ||
        !localcat_identity_equal(&identity, &entry->identity) || localcat_final_path(entry->handle, final_path) != 0 ||
        wcscmp(final_path, entry->final_path) != 0) {
        *diagnostic = "FROZEN_ENTRY.ENTRY_STALE";
        return -1;
    }
    return 0;
}

static int
localcat_loaded_module_reprove(struct localcat_retained_entry *entry, HMODULE module, const char **diagnostic)
{
    wchar_t module_path[32768];
    HANDLE actual_handle;
    struct localcat_file_identity actual_identity;
    wchar_t actual_final_path[32768];
    wchar_t normalized_path[32768];
    DWORD module_path_length;
    module_path_length = GetModuleFileNameW(module, module_path, 32768);
    if (module_path_length == 0 || module_path_length >= 32768) {
        *diagnostic = "FROZEN_ENTRY.ACTUAL_MODULE_PATH_INVALID";
        return -1;
    }
    if (wcsncmp(module_path, L"\\\\?\\", 4) == 0) { wcscpy_s(normalized_path, 32768, module_path); }
    else if (swprintf_s(normalized_path, 32768, L"\\\\?\\%ls", module_path) < 0) {
        *diagnostic = "FROZEN_ENTRY.ACTUAL_MODULE_PATH_INVALID";
        return -1;
    }
    if (CompareStringOrdinal(normalized_path, -1, entry->final_path, -1, TRUE) != CSTR_EQUAL ||
        localcat_open_bound_regular(entry->authority, entry->final_path, &actual_handle, &actual_identity, actual_final_path, diagnostic) != 0) {
        *diagnostic = "FROZEN_ENTRY.ACTUAL_MODULE_PATH_MISMATCH";
        return -1;
    }
    if (!localcat_identity_equal(&actual_identity, &entry->identity) || wcscmp(actual_final_path, entry->final_path) != 0) {
        *diagnostic = "FROZEN_ENTRY.ACTUAL_MODULE_MISMATCH";
        CloseHandle(actual_handle);
        return -1;
    }
    CloseHandle(actual_handle);
    return 0;
}

static int
localcat_native_preload_barrier(struct localcat_bundle_authority *authority, const char **diagnostic)
{
    unsigned int index;
    if (!authority->all_entries_proved) { *diagnostic = "FROZEN_ENTRY.NATIVE_PRELOAD_UNPROVED"; return -1; }
    /* A pathname reopened after DllMain cannot identify an earlier mapped
     * image. Before the first dispatch, reject EVERY preexisting native
     * basename in the closure, even if its pathname and current bytes match.
     * This barrier requires the entry's separately audited E0-E4 single-
     * dispatcher/no-competing-loader-callsite contract; it is not an OS hook. */
    for (index = 0; index < authority->entry_count; ++index) {
        struct localcat_retained_entry *member = &authority->entries[index];
        const wchar_t *basename;
        HMODULE observed;
        unsigned char *bytes;
        size_t size;
        if (member->manifest_entry->role != LOCALCAT_ROLE_NATIVE) { continue; }
        basename = wcsrchr(member->final_path, L'\\');
        if (basename == NULL) { *diagnostic = "FROZEN_ENTRY.NATIVE_PRELOAD_UNPROVED"; return -1; }
        observed = GetModuleHandleW(basename + 1);
        if (member->loaded_module == NULL) {
            if (observed != NULL) { *diagnostic = "FROZEN_ENTRY.NATIVE_PREEXISTING_MODULE"; return -1; }
        } else {
            if (observed != member->loaded_module || !member->actual_module_reproved) {
                *diagnostic = "FROZEN_ENTRY.NATIVE_MODULE_STALE";
                return -1;
            }
            if (localcat_retained_entry_read_verified(member, &bytes, &size, diagnostic) != 0) { return -1; }
            free(bytes);
            if (localcat_loaded_module_reprove(member, observed, diagnostic) != 0) { return -1; }
        }
    }
    return 0;
}

int localcat_initialization_environment_check(const char **diagnostic)
{
    DWORD length, error;
    SetLastError(ERROR_SUCCESS);
    length = GetEnvironmentVariableW(L"__PYVENV_LAUNCHER__", NULL, 0);
    error = GetLastError();
    /* Presence includes an empty value. An ambiguous lookup is not absence. */
    if (length != 0 || error != ERROR_ENVVAR_NOT_FOUND) {
        *diagnostic = "FROZEN_ENTRY.LAUNCHER_ENVIRONMENT_REJECTED";
        return -1;
    }
    return 0;
}

HMODULE
localcat_native_closure_load_verified(struct localcat_retained_entry *entry, const char **diagnostic)
{
    unsigned char *verified_bytes;
    size_t verified_size;
    HMODULE module;
    unsigned int index;
    if (localcat_initialization_environment_check(diagnostic) != 0) return NULL;
    if (entry == NULL || entry->authority == NULL || !entry->authority->all_entries_proved || !entry->digest_proved ||
        (entry->authority->state != 1U && entry->authority->state != 2U) ||
        entry->manifest_entry == NULL || entry->manifest_entry->role != LOCALCAT_ROLE_NATIVE ||
        localcat_retained_entry_read_verified(entry, &verified_bytes, &verified_size, diagnostic) != 0) {
        *diagnostic = "FROZEN_ENTRY.NATIVE_PRELOAD_UNPROVED";
        return NULL;
    }
    free(verified_bytes);
    if (localcat_native_preload_barrier(entry->authority, diagnostic) != 0) { return NULL; }
    if (entry->loaded_module != NULL) { return entry->loaded_module; }
    /* The parser has already rejected cycles. Load declared native dependencies
     * through this same dispatcher before the importer, so OS auto-loading does
     * not create an unattributed "preexisting" module. Never adopt one afterward. */
    for (index = 0; index < entry->manifest_entry->dependency_count; ++index) {
        const unsigned char *encoded = entry->manifest_entry->dependencies + (size_t)index * 4U;
        uint32_t dependency = (uint32_t)encoded[0] | ((uint32_t)encoded[1] << 8) |
                              ((uint32_t)encoded[2] << 16) | ((uint32_t)encoded[3] << 24);
        if (dependency >= entry->authority->entry_count ||
            entry->authority->entries[dependency].manifest_entry->role != LOCALCAT_ROLE_NATIVE) {
            *diagnostic = "FROZEN_ENTRY.NATIVE_DEPENDENCY_INVALID"; return NULL;
        }
    }
    for (index = 0; index < entry->manifest_entry->dependency_count; ++index) {
        const unsigned char *encoded = entry->manifest_entry->dependencies + (size_t)index * 4U;
        uint32_t dependency = (uint32_t)encoded[0] | ((uint32_t)encoded[1] << 8) |
                              ((uint32_t)encoded[2] << 16) | ((uint32_t)encoded[3] << 24);
        if (localcat_native_closure_load_verified(&entry->authority->entries[dependency],diagnostic) == NULL) { return NULL; }
    }
    if (localcat_native_preload_barrier(entry->authority, diagnostic) != 0) { return NULL; }
    module = LoadLibraryExW(entry->final_path, NULL, LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_SYSTEM32);
    if (module == NULL) { *diagnostic = "FROZEN_ENTRY.NATIVE_LOAD_FAILED"; return NULL; }
    if (localcat_loaded_module_reprove(entry, module, diagnostic) != 0) {
        FreeLibrary(module);
        return NULL;
    }
    entry->loaded_module = module;
    entry->actual_module_reproved = 1U;
    return module;
}

static char localcat_native_stage[128];

const char *
localcat_last_stage_marker(void)
{
    return localcat_native_stage;
}

static int
localcat_write_marker(HANDLE stream, const char *marker, DWORD length)
{
    DWORD written;
    if (stream == NULL || stream == INVALID_HANDLE_VALUE ||
        !WriteFile(stream,marker,length,&written,NULL) || written != length ||
        !WriteFile(stream,"\r\n",2,&written,NULL) || written != 2U) return -1;
    return 0;
}

int
localcat_stage_marker(const char *marker)
{
    wchar_t observed[sizeof(localcat_native_stage) + 2U];
    size_t length = 0U, index;
    DWORD saved_error = GetLastError();
    if (marker == NULL) return -1;
    while (length < sizeof(localcat_native_stage) && marker[length] != '\0') {
        unsigned char value = (unsigned char)marker[length];
        if (value < 0x20U || value > 0x7eU) return -1;
        ++length;
    }
    if (length == 0U || length == sizeof(localcat_native_stage)) return -1;
    memcpy(localcat_native_stage,marker,length + 1U);
    for (index = 0U; index < length; ++index) observed[index] = (wchar_t)(unsigned char)marker[index];
    observed[length] = L'\r'; observed[length + 1U] = L'\n'; observed[length + 2U] = L'\0';
    /* State is recorded before observers or a following non-returning call.
     * Neither output channel grants or revokes bundle authority. */
    (void)localcat_write_marker(GetStdHandle(STD_ERROR_HANDLE),marker,(DWORD)length);
    OutputDebugStringW(observed);
    SetLastError(saved_error);
    return 0;
}

void
localcat_diagnostic_marker(const char *marker)
{
    if (localcat_write_marker(GetStdHandle(STD_ERROR_HANDLE),marker,(DWORD)strlen(marker)) == 0) return;
    MessageBoxA(NULL, marker, "LocalCAT", MB_OK | MB_ICONERROR);
}

#endif
