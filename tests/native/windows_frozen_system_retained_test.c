#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <string.h>
#include "localcat_native_closure.h"
#include "system_candidates.h"
static unsigned int volume_fault, drive_calls, volume_calls;
static HANDLE expected_volume_handle;

UINT WINAPI test_drive_type(LPCWSTR path)
{
    ++drive_calls;
    if (volume_fault == 1U) { return DRIVE_REMOTE; }
    if (volume_fault == 2U) { return DRIVE_REMOVABLE; }
    return GetDriveTypeW(path);
}

BOOL WINAPI test_volume_information(HANDLE handle, LPWSTR label, DWORD label_size,
    LPDWORD serial, LPDWORD maximum, LPDWORD flags, LPWSTR filesystem, DWORD filesystem_size)
{
    BOOL result;
    ++volume_calls;
    if (expected_volume_handle != NULL && handle != expected_volume_handle) { return FALSE; }
    result = GetVolumeInformationByHandleW(handle, label, label_size, serial, maximum, flags, filesystem, filesystem_size);
    if (!result) { return result; }
    if (volume_fault == 3U) { wcscpy_s(filesystem, filesystem_size, L"ReFS"); }
    if (volume_fault == 4U) { wcscpy_s(filesystem, filesystem_size, L"FAT32"); }
    if (volume_fault == 5U) { *maximum = 254U; }
    if (volume_fault == 6U) { *flags &= ~FILE_PERSISTENT_ACLS; }
    if (volume_fault == 7U) { *flags &= ~FILE_SUPPORTS_REPARSE_POINTS; }
    if (volume_fault == 8U) { return FALSE; }
    return result;
}

#define GetDriveTypeW test_drive_type
#define GetVolumeInformationByHandleW test_volume_information
/* White-box fault injection changes retained proof metadata, never OS files. */
#include "localcat_native_closure.c"
#undef GetDriveTypeW
#undef GetVolumeInformationByHandleW

#define CHECK(x) do { if (!(x)) { fprintf(stderr,"FAIL line %d: %s; diagnostic=%s\n",__LINE__,#x,diagnostic ? diagnostic : "none"); return 1; } } while (0)

int main(void)
{
    struct localcat_system_files *files = NULL;
    struct localcat_system_file_candidate bad[2];
    const wchar_t *names[] = {L"",L"../bcrypt.dll",L"sub\\bcrypt.dll",L"C:\\bcrypt.dll",
        L"bcrypt.dll:stream",L"bcrypt.dll.",L"BCRYPT.dll",L"bcrypt",L".dll",L"bcrypt .dll"};
    const char *diagnostic = NULL;
    DWORD before, after;
    FILE_ID_INFO identity;
    HANDLE directory;
    size_t index;
    CHECK(GetProcessHandleCount(GetCurrentProcess(), &before));
    for (volume_fault = 1U; volume_fault <= 8U; ++volume_fault) {
        CHECK(localcat_system_files_prepare(system_candidates, 2U, &files, &diagnostic) != 0);
        CHECK(files == NULL);
    }
    volume_fault = 0U;
    CHECK(localcat_system_files_prepare(system_candidates, 2U, &files, &diagnostic) == 0);
    CHECK(files != NULL);
    CHECK(drive_calls > 0U && volume_calls > 0U);
    drive_calls = volume_calls = 0U;
    expected_volume_handle = files->directories[0].handle;
    CHECK(localcat_system_files_reprove(files, &diagnostic) == 0);
    CHECK(drive_calls > 0U && volume_calls > 0U);
    expected_volume_handle = NULL;
    CHECK(files->member_count == 2U && files->directory_count >= 2U);
    CHECK(GetFileInformationByHandleEx(files->members[0].handle, FileIdInfo, &identity, sizeof(identity)));
    CHECK(same_identity(&identity, &files->members[0].identity));
    CHECK(localcat_system_files_prepare(system_candidates, 2U, &files, &diagnostic) != 0);
    CHECK(localcat_system_files_reprove(files, &diagnostic) == 0);
    localcat_system_files_close(&files);
    for (index = 1U; index <= 8U; ++index) {
        CHECK(localcat_system_files_prepare(system_candidates, 2U, &files, &diagnostic) == 0);
        volume_fault = (unsigned int)index;
        CHECK(localcat_system_files_reprove(files, &diagnostic) != 0);
        volume_fault = 0U;
        CHECK(localcat_system_files_reprove(files, &diagnostic) != 0);
        localcat_system_files_close(&files);
    }
    CHECK(localcat_system_files_prepare(system_candidates, 2U, &files, &diagnostic) == 0);
    files->members[0].identity.FileId.Identifier[0] ^= 1U;
    CHECK(localcat_system_files_reprove(files, &diagnostic) != 0);
    files->members[0].identity.FileId.Identifier[0] ^= 1U;
    CHECK(localcat_system_files_reprove(files, &diagnostic) != 0); /* sticky revocation */
    localcat_system_files_close(&files);
    CHECK(localcat_system_files_prepare(system_candidates, 2U, &files, &diagnostic) == 0);
    files->members[1].digest[0] ^= 1U;
    CHECK(localcat_system_files_reprove(files, &diagnostic) != 0);
    localcat_system_files_close(&files);
    CHECK(localcat_system_files_prepare(system_candidates, 2U, &files, &diagnostic) == 0);
    directory = files->directories[0].handle;
    files->directories[0].handle = INVALID_HANDLE_VALUE;
    CHECK(localcat_system_files_reprove(files, &diagnostic) != 0);
    files->directories[0].handle = directory;
    localcat_system_files_close(&files);
    CHECK(files == NULL);
    CHECK(localcat_system_files_reprove(files, &diagnostic) != 0);
    localcat_system_files_close(&files);
    for (index = 0; index < sizeof(names)/sizeof(names[0]); ++index) {
        memcpy(bad, system_candidates, sizeof(bad)); bad[1].basename = names[index];
        CHECK(localcat_system_files_prepare(bad, 2U, &files, &diagnostic) != 0);
        CHECK(files == NULL);
    }
    memcpy(bad, system_candidates, sizeof(bad)); bad[1].sha256[0] ^= 1U;
    CHECK(localcat_system_files_prepare(bad, 2U, &files, &diagnostic) != 0);
    CHECK(files == NULL);
    memcpy(bad, system_candidates, sizeof(bad)); ++bad[1].size;
    CHECK(localcat_system_files_prepare(bad, 2U, &files, &diagnostic) != 0);
    CHECK(files == NULL);
    memcpy(bad, system_candidates, sizeof(bad)); bad[1].size = 0U;
    CHECK(localcat_system_files_prepare(bad, 2U, &files, &diagnostic) != 0);
    CHECK(files == NULL);
    memcpy(bad, system_candidates, sizeof(bad)); bad[1].basename = bad[0].basename;
    CHECK(localcat_system_files_prepare(bad, 2U, &files, &diagnostic) != 0);
    CHECK(files == NULL);
    memcpy(bad, system_candidates, sizeof(bad)); bad[1].basename = L"localcat-missing-candidate-82972.dll";
    CHECK(localcat_system_files_prepare(bad, 2U, &files, &diagnostic) != 0);
    CHECK(files == NULL);
    CHECK(localcat_system_files_prepare(NULL, 2U, &files, &diagnostic) != 0);
    CHECK(localcat_system_files_prepare(system_candidates, 0U, &files, &diagnostic) != 0);
    CHECK(localcat_system_files_prepare(system_candidates, 33U, &files, &diagnostic) != 0);
    CHECK(localcat_system_files_prepare(system_candidates, 2U, NULL, &diagnostic) != 0);
    CHECK(localcat_system_files_reprove(NULL, NULL) != 0);
    localcat_system_files_close(NULL);
    CHECK(GetProcessHandleCount(GetCurrentProcess(), &after));
    CHECK(before == after);
    puts("system retained: FILE_PROOF_ONLY positive, bad candidate, cleanup, closed rejection");
    return 0;
}
