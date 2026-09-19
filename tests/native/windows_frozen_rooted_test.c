/* Include the implementation to exercise failure boundaries without exporting
 * test hooks or introducing a production authority-minting API. */
#include <windows.h>
static int inject_wrong_module_path;
static int inject_marker_write, marker_observer_disabled;
static unsigned int marker_write_calls, marker_observer_calls, marker_state_observed, diagnostic_dialog_calls;
static DWORD test_module_filename(HMODULE module, LPWSTR path, DWORD capacity);
static BOOL WINAPI test_write_file(HANDLE file, LPCVOID bytes, DWORD count, LPDWORD written, LPOVERLAPPED overlap);
static void WINAPI test_debug_string(LPCWSTR marker);
static int WINAPI test_message_box(HWND window, LPCSTR text, LPCSTR caption, UINT type);
#define GetModuleFileNameW test_module_filename
#define WriteFile test_write_file
#define OutputDebugStringW test_debug_string
#define MessageBoxA test_message_box
#include "localcat_rooted_io.c"
#undef GetModuleFileNameW
#undef WriteFile
#undef OutputDebugStringW
#undef MessageBoxA

static BOOL WINAPI test_write_file(HANDLE file, LPCVOID bytes, DWORD count, LPDWORD written, LPOVERLAPPED overlap)
{
    if (inject_marker_write != 0) {
        ++marker_write_calls;
        SetLastError(ERROR_WRITE_FAULT);
        if (inject_marker_write == 1) return FALSE;
        *written = ((inject_marker_write == 2 && marker_write_calls == 1U) ||
                    (inject_marker_write == 3 && marker_write_calls == 2U)) ? count - 1U : count;
        return TRUE;
    }
    return WriteFile(file,bytes,count,written,overlap);
}

static void WINAPI test_debug_string(LPCWSTR marker)
{
    const char *recorded = localcat_last_stage_marker();
    size_t index = 0U;
    ++marker_observer_calls;
    if (marker_observer_disabled) return;
    while (recorded[index] != '\0' && marker[index] == (wchar_t)(unsigned char)recorded[index]) ++index;
    if (recorded[index] == '\0' && wcscmp(marker + index,L"\r\n") == 0) ++marker_state_observed;
}

static int WINAPI test_message_box(HWND window, LPCSTR text, LPCSTR caption, UINT type)
{
    (void)window; (void)text; (void)caption; (void)type;
    ++diagnostic_dialog_calls;
    return IDOK;
}

static DWORD test_module_filename(HMODULE module, LPWSTR path, DWORD capacity)
{
    return GetModuleFileNameW(inject_wrong_module_path && module != NULL ? NULL : module, path, capacity);
}

static unsigned int checks, failures;

static void check(int condition, const char *name)
{
    ++checks;
    if (!condition) { ++failures; printf("FAIL %s\n", name); }
}

static void check_unpinned(const wchar_t *path)
{
    HANDLE writable = CreateFileW(path, GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        NULL, OPEN_EXISTING, 0, NULL);
    check(writable != INVALID_HANDLE_VALUE, "failed/closed authority releases source handles");
    if (writable != INVALID_HANDLE_VALUE) { CloseHandle(writable); }
}

static void check_stage_observers(struct localcat_bundle_authority *authority, HANDLE readonly_file)
{
    struct localcat_bundle_authority *snapshot = (struct localcat_bundle_authority *)malloc(sizeof(*snapshot));
    HANDLE original = GetStdHandle(STD_ERROR_HANDLE);
    HANDLE streams[] = {NULL, INVALID_HANDLE_VALUE, readonly_file, original};
    unsigned int index;
    char mutable_marker[] = "TEST_ONLY.NATIVE_STAGE_RECORDED";
    char oversized[129];
    check(snapshot != NULL,"allocate authority snapshot for observer isolation");
    if (snapshot == NULL) return;
    memcpy(snapshot,authority,sizeof(*snapshot));
    for (index = 0U; index < sizeof(streams) / sizeof(streams[0]); ++index) {
        check(SetStdHandle(STD_ERROR_HANDLE,streams[index]),"install optional stderr test mode");
        SetLastError(ERROR_ACCESS_DENIED);
        check(localcat_stage_marker(mutable_marker) == 0,"absent/invalid/readonly stderr does not reject stage");
        check(GetLastError() == ERROR_ACCESS_DENIED,"observer failures preserve caller error state");
        check(strcmp(localcat_last_stage_marker(),mutable_marker) == 0,"native stage remains readable without stderr");
    }
    check(SetStdHandle(STD_ERROR_HANDLE,original),"restore stderr");
    for (inject_marker_write = 1; inject_marker_write <= 3; ++inject_marker_write) {
        marker_write_calls = 0U;
        check(localcat_stage_marker(mutable_marker) == 0,"failed/short body/short newline writes are optional");
    }
    inject_marker_write = 0;
    check(marker_observer_calls == 7U && marker_state_observed == 7U,"debug observer sees already-recorded native state");
    marker_observer_disabled = 1;
    check(localcat_stage_marker(mutable_marker) == 0,"no debug consumer does not reject stage");
    marker_observer_disabled = 0;
    mutable_marker[0] = 'X';
    check(strcmp(localcat_last_stage_marker(),"TEST_ONLY.NATIVE_STAGE_RECORDED") == 0,"stage owns a stable copy, not caller memory");
    memset(oversized,'X',sizeof(oversized)); oversized[sizeof(oversized)-1U] = '\0';
    check(localcat_stage_marker(NULL) != 0 && localcat_stage_marker("") != 0 &&
          localcat_stage_marker(oversized) != 0 && localcat_stage_marker("line\nbreak") != 0,
          "invalid marker inputs are rejected without observation");
    check(strcmp(localcat_last_stage_marker(),"TEST_ONLY.NATIVE_STAGE_RECORDED") == 0 &&
          marker_observer_calls == 8U,"invalid marker preserves previous native stage");
    check(memcmp(snapshot,authority,sizeof(*snapshot)) == 0,"observer outcomes do not alter retained authority");
    check(diagnostic_dialog_calls == 0U,"progress never opens a modal dialog");
    inject_marker_write = 3; marker_write_calls = 0U;
    localcat_diagnostic_marker("TEST_ONLY.REAL_FAILURE");
    inject_marker_write = 0;
    check(diagnostic_dialog_calls == 1U,"real failure still has modal fallback after incomplete stderr output");
    free(snapshot);
}

int main(int argc, char **argv)
{
    struct localcat_bundle_authority *authority = (struct localcat_bundle_authority *)calloc(1, sizeof(*authority));
    struct localcat_retained_entry *source, *native;
    const char *diagnostic = NULL;
    unsigned char *bytes = NULL;
    size_t size = 0;
    wchar_t source_path[32768], parent_path[32768], renamed_path[32768];
    HANDLE attempt;
    int prepared;
    if (authority == NULL || argc != 2) { return 2; }
    if (strncmp(argv[1], "preexisting", 11) == 0) {
        wchar_t native_path[32768], old_path[32768], backup_path[32768];
        wchar_t *separator;
        HMODULE foreign;
        int swap = strcmp(argv[1], "preexisting-swap") == 0;
        GetModuleFileNameW(NULL, source_path, 32768);
        separator = wcsrchr(source_path, L'\\');
        if (separator == NULL) { free(authority); return 2; }
        *separator = 0;
        swprintf_s(native_path, 32768, L"%ls\\_internal\\lib\\%ls", source_path,
            strcmp(argv[1], "preexisting-other") == 0 ? L"other.dll" : L"probe.dll");
        swprintf_s(old_path, 32768, L"%ls\\..\\probe-old-image.dll", source_path);
        swprintf_s(backup_path, 32768, L"%ls\\..\\probe-current.dll", source_path);
        if (swap) {
            DWORD written;
            check(CopyFileW(native_path, backup_path, TRUE), "preserve exact current DLL bytes");
            attempt = CreateFileW(native_path, FILE_APPEND_DATA, 0, NULL, OPEN_EXISTING, 0, NULL);
            check(attempt != INVALID_HANDLE_VALUE, "prepare distinct old DLL bytes");
            if (attempt != INVALID_HANDLE_VALUE) {
                check(WriteFile(attempt, "old-image-overlay", 17, &written, NULL) && written == 17, "append old DLL overlay");
                CloseHandle(attempt);
            }
        }
        foreign = LoadLibraryExW(native_path, NULL, LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_SYSTEM32);
        check(foreign != NULL, "preload a DLL outside the dispatcher");
        if (swap) {
            BOOL renamed = MoveFileExW(native_path, old_path, 0);
            check(renamed != 0, "rename still-loaded old DLL image");
            if (!renamed) {
                printf("old-image rename denied by host: %lu\n", GetLastError());
                if (foreign != NULL) { FreeLibrary(foreign); }
                free(authority); return 1;
            }
            check(CopyFileW(backup_path, native_path, TRUE), "restore new manifest-matching DLL at old pathname");
        }
        prepared = localcat_bundle_authority_prepare(authority, &diagnostic);
        check(prepared != 0 && strcmp(diagnostic, "FROZEN_ENTRY.NATIVE_PREEXISTING_MODULE") == 0,
            "preexisting native image rejected even when current pathname bytes match manifest");
        localcat_bundle_authority_close(authority);
        if (foreign != NULL) { FreeLibrary(foreign); }
        free(authority);
        printf("rooted I/O: %u checks, %u failures (%s)\n", checks, failures, argv[1]);
        return failures ? 1 : 0;
    }
    prepared = localcat_bundle_authority_prepare(authority, &diagnostic);
    if (strcmp(argv[1], "negative") == 0) {
        check(prepared != 0, "tamper/missing/reparse must fail prepare");
        check(authority->state == 3U && authority->entry_count == 0U && authority->directory_count == 0U,
            "failed prepare closes every retained authority");
        if (swprintf_s(source_path, 32768, L"%ls\\_internal\\code\\critical.py", authority->root_final_path) > 0 &&
            GetFileAttributesW(source_path) != INVALID_FILE_ATTRIBUTES) { check_unpinned(source_path); }
        if (prepared == 0) { localcat_bundle_authority_close(authority); }
        printf("rooted negative diagnostic: %s\n", diagnostic ? diagnostic : "missing");
    } else {
        check(prepared == 0, "valid local NTFS bundle prepares");
        if (prepared != 0) { printf("prepare diagnostic: %s error=%lu\n", diagnostic, GetLastError()); free(authority); return 1; }
        source = localcat_bundle_authority_find(authority, "critical-source");
        native = localcat_bundle_authority_find(authority, "native-probe");
        check(source != NULL && native != NULL, "manifest entries retained");
        if (source == NULL || native == NULL) { localcat_bundle_authority_close(authority); free(authority); return 1; }
        check_stage_observers(authority,source->handle);
        wcscpy_s(source_path, 32768, source->final_path);
        swprintf_s(parent_path, 32768, L"%ls\\_internal\\code", authority->root_final_path);
        swprintf_s(renamed_path, 32768, L"%ls\\_internal\\renamed-code", authority->root_final_path);
        check(authority->all_entries_proved && source->digest_proved && native->digest_proved,
            "every input hash proved before first native load");
        {
            wchar_t extra_path[32768];
            swprintf_s(extra_path, 32768, L"%ls\\_internal\\code\\late-extra.py", authority->root_final_path);
            attempt = CreateFileW(extra_path, GENERIC_WRITE, 0, NULL, CREATE_NEW, 0, NULL);
            check(attempt != INVALID_HANDLE_VALUE, "create a late extra after initial inventory");
            if (attempt != INVALID_HANDLE_VALUE) { CloseHandle(attempt); }
            check(localcat_retained_entry_read_verified(source, &bytes, &size, &diagnostic) != 0 &&
                strcmp(diagnostic, "FROZEN_ENTRY.INVENTORY_EXTRA") == 0,
                "late extra prevents trusted source read");
            check(localcat_native_closure_load_verified(native, &diagnostic) == NULL,
                "late extra prevents first native dispatch");
            check(DeleteFileW(extra_path) != 0, "remove isolated late-extra test fixture");
        }
        attempt = CreateFileW(authority->manifest_final_path, GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, NULL, OPEN_EXISTING, 0, NULL);
        check(attempt == INVALID_HANDLE_VALUE && GetLastError() == ERROR_SHARING_VIOLATION,
            "runtime manifest handle remains pinned after parsing");
        if (attempt != INVALID_HANDLE_VALUE) { CloseHandle(attempt); }
        check(localcat_retained_entry_read_verified(source, &bytes, &size, &diagnostic) == 0,
            "read retained source");
        if (bytes != NULL) { check(bytes[size] == 0, "C compiler input is NUL terminated"); free(bytes); }
        attempt = CreateFileW(source_path, GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            NULL, OPEN_EXISTING, 0, NULL);
        check(attempt == INVALID_HANDLE_VALUE && GetLastError() == ERROR_SHARING_VIOLATION, "source overwrite denied");
        if (attempt != INVALID_HANDLE_VALUE) { CloseHandle(attempt); }
        check(!MoveFileExW(parent_path, renamed_path, 0) && GetLastError() == ERROR_SHARING_VIOLATION,
            "intermediate directory rename denied");
        authority->all_entries_proved = 0U;
        check(localcat_native_closure_load_verified(native, &diagnostic) == NULL, "partial closure cannot dispatch");
        authority->all_entries_proved = 1U;
        {
            struct localcat_retained_entry *other = localcat_bundle_authority_find(authority, "other-native");
            HMODULE foreign = other ? LoadLibraryExW(other->final_path, NULL,
                LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_SYSTEM32) : NULL;
            check(foreign != NULL, "inject another closure member after initial barrier");
            check(localcat_native_closure_load_verified(native, &diagnostic) == NULL &&
                strcmp(diagnostic, "FROZEN_ENTRY.NATIVE_PREEXISTING_MODULE") == 0,
                "every dispatch repeats the barrier for the complete closure");
            if (foreign != NULL) { FreeLibrary(foreign); }
        }
        native->manifest_entry = &authority->manifest.entries[0];
        authority->manifest.entries[0].digest[0] ^= 1U;
        check(localcat_native_closure_load_verified(native, &diagnostic) == NULL, "digest mismatch rejected before dispatch");
        authority->manifest.entries[0].digest[0] ^= 1U;
        inject_wrong_module_path = 1;
        check(localcat_native_closure_load_verified(native, &diagnostic) == NULL &&
            strcmp(diagnostic, "FROZEN_ENTRY.ACTUAL_MODULE_PATH_MISMATCH") == 0,
            "module pathname mismatch rejected before rooted reopen");
        inject_wrong_module_path = 0;
        check(localcat_native_closure_load_verified(native, &diagnostic) != NULL && native->actual_module_reproved,
            "real DLL restricted load and actual-path FileId reproof");
        check(localcat_native_closure_load_verified(native, &diagnostic) == native->loaded_module,
            "own prior dispatcher load remains available after renewed proof");
        inject_wrong_module_path = 1;
        check(localcat_native_closure_load_verified(native, &diagnostic) == NULL,
            "own prior dispatcher load must renew actual-module proof");
        inject_wrong_module_path = 0;
        /* Corrupt a retained parent's expected identity to exercise a live
         * reproof failure independently of the OS rename-sharing guard. */
        authority->directory_identities[authority->directory_count - 1U].file_id[0] ^= 1U;
        check(localcat_retained_entry_read_verified(source, &bytes, &size, &diagnostic) != 0,
            "stale retained parent denies bytes");
        authority->directory_identities[authority->directory_count - 1U].file_id[0] ^= 1U;
        localcat_bundle_authority_close(authority);
        check(source->final_path == NULL && native->final_path == NULL && authority->manifest_bytes == NULL,
            "close releases allocated paths and manifest while entry slots remain safely revoked");
        check(localcat_retained_entry_read_verified(source, &bytes, &size, &diagnostic) != 0,
            "held entry reference fails after close");
        check(localcat_native_closure_load_verified(native, &diagnostic) == NULL,
            "held native entry reference fails after close");
        check_unpinned(source_path);
        /* Build a test-only NUL source and retain its exact digest. This tests
         * the reader's source policy independently of manifest hash rejection. */
        {
            unsigned char nul_source[] = {'x', '=', 0, '\n'};
            struct localcat_sha256_context hash;
            DWORD written;
            attempt = CreateFileW(source_path, GENERIC_WRITE, 0, NULL, TRUNCATE_EXISTING, 0, NULL);
            check(attempt != INVALID_HANDLE_VALUE, "create isolated NUL source fixture");
            if (attempt != INVALID_HANDLE_VALUE) {
                check(WriteFile(attempt, nul_source, sizeof(nul_source), &written, NULL) && written == sizeof(nul_source), "write NUL source");
                CloseHandle(attempt);
            }
            memset(authority, 0, sizeof(*authority));
            check(localcat_bind_bundle_root(authority, &diagnostic) == 0 && localcat_open_manifest(authority, &diagnostic) == 0,
                "bind test NUL source manifest");
            localcat_sha256_init(&hash); localcat_sha256_update(&hash, nul_source, sizeof(nul_source));
            localcat_sha256_final(&hash, authority->manifest.entries[2].digest);
            authority->manifest.entries[2].byte_count = sizeof(nul_source);
            check(localcat_open_manifest_entries(authority, &diagnostic) != 0 &&
                strcmp(diagnostic, "FROZEN_ENTRY.SOURCE_NUL_REJECTED") == 0, "exact-hash source with embedded NUL rejected");
            localcat_bundle_authority_close(authority);
            check_unpinned(source_path);
        }
    }
    localcat_bundle_authority_close(authority);
    free(authority);
    printf("rooted I/O: %u checks, %u failures\n", checks, failures);
    return failures ? 1 : 0;
}
