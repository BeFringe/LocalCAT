#define WIN32_LEAN_AND_MEAN
#define UNICODE
#define _UNICODE
#include <windows.h>

#define LOCALCAT_HELPER_MAGIC 0x5441434cUL
#define LOCALCAT_HELPER_VERSION 2UL
#define LOCALCAT_MAX_SID_BYTES 68UL
#define LOCALCAT_MAX_RESTRICTED_SIDS 16UL

#define LOCALCAT_RESULT_NOT_RUN 0UL
#define LOCALCAT_RESULT_OK 1UL
#define LOCALCAT_RESULT_ERROR 2UL

#define LOCALCAT_PHASE_OPEN 1UL
#define LOCALCAT_PHASE_SEEK 2UL
#define LOCALCAT_PHASE_WRITE 3UL
#define LOCALCAT_PHASE_SET_EOF 4UL
#define LOCALCAT_PHASE_DELETE 5UL

#define LOCALCAT_STATUS_OK 0UL
#define LOCALCAT_STATUS_COMMAND_LINE 1UL
#define LOCALCAT_STATUS_OPEN_TOKEN 2UL
#define LOCALCAT_STATUS_TOKEN_USER 3UL
#define LOCALCAT_STATUS_TOKEN_TYPE 4UL
#define LOCALCAT_STATUS_TOKEN_ELEVATION 5UL
#define LOCALCAT_STATUS_TOKEN_INTEGRITY 6UL
#define LOCALCAT_STATUS_TOKEN_RESTRICTIONS 7UL
#define LOCALCAT_STATUS_SID_SHAPE 8UL
#define LOCALCAT_STATUS_TOKEN_SANDBOX 9UL
#define LOCALCAT_STATUS_TOKEN_HAS_RESTRICTIONS 10UL
#define LOCALCAT_STATUS_CLOSE 11UL

typedef struct LocalCatSidSlot {
    DWORD length;
    BYTE bytes[LOCALCAT_MAX_SID_BYTES];
} LocalCatSidSlot;

typedef struct LocalCatOperationResult {
    DWORD status;
    DWORD phase;
    DWORD winerror;
    DWORD transferred;
} LocalCatOperationResult;

typedef struct LocalCatNativeEvidence {
    DWORD magic;
    DWORD version;
    DWORD record_size;
    DWORD helper_status;
    DWORD helper_winerror;
    DWORD token_type;
    DWORD elevation_type;
    DWORD integrity_rid;
    DWORD is_restricted;
    DWORD sandbox_inert;
    DWORD has_restrictions;
    DWORD user_sid_length;
    DWORD integrity_sid_length;
    DWORD restricted_sid_count;
    BYTE user_sid[LOCALCAT_MAX_SID_BYTES];
    BYTE integrity_sid[LOCALCAT_MAX_SID_BYTES];
    LocalCatSidSlot restricted_sids[LOCALCAT_MAX_RESTRICTED_SIDS];
    LocalCatOperationResult read_result;
    LocalCatOperationResult open_result;
    LocalCatOperationResult write_result;
    LocalCatOperationResult delete_result;
} LocalCatNativeEvidence;

static LocalCatNativeEvidence evidence;
static WCHAR target_path[32768];
static HANDLE report_output = NULL;
static BYTE token_user_buffer[512];
static BYTE token_integrity_buffer[512];
static BYTE token_restrictions_buffer[8192];
static const BYTE write_payload[] = {
    'n', 'a', 't', 'i', 'v', 'e', '-', 'w', 'o', 'r', 'k', 'e', 'r', '-',
    'w', 'r', 'i', 't', 'e'
};

static void copy_bytes(BYTE *destination, const BYTE *source, DWORD count) {
    DWORD index;
    for (index = 0; index < count; ++index) {
        destination[index] = source[index];
    }
}

static BOOL copy_sid_bytes(BYTE *destination, DWORD *destination_length, PSID sid) {
    DWORD length = GetLengthSid(sid);
    if (length == 0 || length > LOCALCAT_MAX_SID_BYTES) {
        return FALSE;
    }
    copy_bytes(destination, (const BYTE *)sid, length);
    *destination_length = length;
    return TRUE;
}

static DWORD sid_integrity_rid(const BYTE *sid, DWORD length) {
    DWORD count;
    DWORD offset;
    if (length < 12 || sid[0] != 1) {
        return 0xffffffffUL;
    }
    count = sid[1];
    if (count == 0 || length != 8 + count * 4) {
        return 0xffffffffUL;
    }
    offset = 8 + (count - 1) * 4;
    return ((DWORD)sid[offset])
        | ((DWORD)sid[offset + 1] << 8)
        | ((DWORD)sid[offset + 2] << 16)
        | ((DWORD)sid[offset + 3] << 24);
}

static BOOL parse_command_line(void) {
    const WCHAR *cursor = GetCommandLineW();
    ULONG_PTR output_value = 0;
    DWORD length = 0;
    BOOL saw_digit = FALSE;
    BOOL quoted;
    if (cursor == NULL) {
        return FALSE;
    }
    while (*cursor == L' ' || *cursor == L'\t') {
        ++cursor;
    }
    quoted = (*cursor == L'"');
    if (quoted) {
        ++cursor;
        while (*cursor != L'\0' && *cursor != L'"') {
            ++cursor;
        }
        if (*cursor != L'"') {
            return FALSE;
        }
        ++cursor;
    } else {
        while (*cursor != L'\0' && *cursor != L' ' && *cursor != L'\t') {
            ++cursor;
        }
    }
    while (*cursor == L' ' || *cursor == L'\t') {
        ++cursor;
    }
    while (*cursor >= L'0' && *cursor <= L'9') {
        ULONG_PTR digit = (ULONG_PTR)(*cursor - L'0');
        if (output_value > (((ULONG_PTR)-1 - digit) / 10)) {
            return FALSE;
        }
        output_value = output_value * 10 + digit;
        saw_digit = TRUE;
        ++cursor;
    }
    if (!saw_digit || output_value == 0) {
        return FALSE;
    }
    report_output = (HANDLE)output_value;
    while (*cursor == L' ' || *cursor == L'\t') {
        ++cursor;
    }
    if (*cursor != L'"') {
        return FALSE;
    }
    ++cursor;
    while (*cursor != L'\0' && *cursor != L'"') {
        if (length + 1 >= (DWORD)(sizeof(target_path) / sizeof(target_path[0]))) {
            return FALSE;
        }
        target_path[length++] = *cursor++;
    }
    if (*cursor != L'"' || length == 0) {
        return FALSE;
    }
    target_path[length] = L'\0';
    ++cursor;
    while (*cursor == L' ' || *cursor == L'\t') {
        ++cursor;
    }
    return *cursor == L'\0';
}

static BOOL capture_token(HANDLE token) {
    DWORD returned = 0;
    TOKEN_USER *token_user;
    TOKEN_MANDATORY_LABEL *integrity;
    TOKEN_GROUPS *restrictions;
    DWORD index;
    if (!GetTokenInformation(
            token,
            TokenUser,
            token_user_buffer,
            sizeof(token_user_buffer),
            &returned)) {
        evidence.helper_status = LOCALCAT_STATUS_TOKEN_USER;
        evidence.helper_winerror = GetLastError();
        return FALSE;
    }
    token_user = (TOKEN_USER *)token_user_buffer;
    if (!copy_sid_bytes(
            evidence.user_sid,
            &evidence.user_sid_length,
            token_user->User.Sid)) {
        evidence.helper_status = LOCALCAT_STATUS_SID_SHAPE;
        evidence.helper_winerror = GetLastError();
        return FALSE;
    }

    returned = 0;
    if (!GetTokenInformation(
            token,
            TokenType,
            &evidence.token_type,
            sizeof(evidence.token_type),
            &returned)) {
        evidence.helper_status = LOCALCAT_STATUS_TOKEN_TYPE;
        evidence.helper_winerror = GetLastError();
        return FALSE;
    }
    returned = 0;
    if (!GetTokenInformation(
            token,
            TokenElevationType,
            &evidence.elevation_type,
            sizeof(evidence.elevation_type),
            &returned)) {
        evidence.helper_status = LOCALCAT_STATUS_TOKEN_ELEVATION;
        evidence.helper_winerror = GetLastError();
        return FALSE;
    }
    returned = 0;
    if (!GetTokenInformation(
            token,
            TokenIntegrityLevel,
            token_integrity_buffer,
            sizeof(token_integrity_buffer),
            &returned)) {
        evidence.helper_status = LOCALCAT_STATUS_TOKEN_INTEGRITY;
        evidence.helper_winerror = GetLastError();
        return FALSE;
    }
    integrity = (TOKEN_MANDATORY_LABEL *)token_integrity_buffer;
    if (!copy_sid_bytes(
            evidence.integrity_sid,
            &evidence.integrity_sid_length,
            integrity->Label.Sid)) {
        evidence.helper_status = LOCALCAT_STATUS_SID_SHAPE;
        evidence.helper_winerror = GetLastError();
        return FALSE;
    }
    evidence.integrity_rid = sid_integrity_rid(
        evidence.integrity_sid,
        evidence.integrity_sid_length);
    if (evidence.integrity_rid == 0xffffffffUL) {
        evidence.helper_status = LOCALCAT_STATUS_SID_SHAPE;
        evidence.helper_winerror = ERROR_INVALID_SID;
        return FALSE;
    }

    evidence.is_restricted = IsTokenRestricted(token) ? 1UL : 0UL;
    returned = 0;
    if (!GetTokenInformation(
            token,
            TokenSandBoxInert,
            &evidence.sandbox_inert,
            sizeof(evidence.sandbox_inert),
            &returned)) {
        evidence.helper_status = LOCALCAT_STATUS_TOKEN_SANDBOX;
        evidence.helper_winerror = GetLastError();
        return FALSE;
    }
    returned = 0;
    if (!GetTokenInformation(
            token,
            TokenHasRestrictions,
            &evidence.has_restrictions,
            sizeof(evidence.has_restrictions),
            &returned)) {
        evidence.helper_status = LOCALCAT_STATUS_TOKEN_HAS_RESTRICTIONS;
        evidence.helper_winerror = GetLastError();
        return FALSE;
    }
    returned = 0;
    if (!GetTokenInformation(
            token,
            TokenRestrictedSids,
            token_restrictions_buffer,
            sizeof(token_restrictions_buffer),
            &returned)) {
        evidence.helper_status = LOCALCAT_STATUS_TOKEN_RESTRICTIONS;
        evidence.helper_winerror = GetLastError();
        return FALSE;
    }
    restrictions = (TOKEN_GROUPS *)token_restrictions_buffer;
    if (restrictions->GroupCount > LOCALCAT_MAX_RESTRICTED_SIDS) {
        evidence.helper_status = LOCALCAT_STATUS_TOKEN_RESTRICTIONS;
        evidence.helper_winerror = ERROR_INSUFFICIENT_BUFFER;
        return FALSE;
    }
    evidence.restricted_sid_count = restrictions->GroupCount;
    for (index = 0; index < restrictions->GroupCount; ++index) {
        if (!copy_sid_bytes(
                evidence.restricted_sids[index].bytes,
                &evidence.restricted_sids[index].length,
                restrictions->Groups[index].Sid)) {
            evidence.helper_status = LOCALCAT_STATUS_SID_SHAPE;
            evidence.helper_winerror = GetLastError();
            return FALSE;
        }
    }
    return TRUE;
}

static void run_read_operation(void) {
    HANDLE handle = CreateFileW(
        target_path,
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        NULL,
        OPEN_EXISTING,
        FILE_FLAG_OPEN_REPARSE_POINT,
        NULL);
    evidence.read_result.phase = LOCALCAT_PHASE_OPEN;
    if (handle == INVALID_HANDLE_VALUE) {
        evidence.read_result.status = LOCALCAT_RESULT_ERROR;
        evidence.read_result.winerror = GetLastError();
        return;
    }
    evidence.read_result.status = LOCALCAT_RESULT_OK;
    if (!CloseHandle(handle) && evidence.helper_status == LOCALCAT_STATUS_OK) {
        evidence.helper_status = LOCALCAT_STATUS_CLOSE;
        evidence.helper_winerror = GetLastError();
    }
}

static void run_open_operation(void) {
    HANDLE handle = CreateFileW(
        target_path,
        GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        NULL,
        OPEN_EXISTING,
        FILE_FLAG_OPEN_REPARSE_POINT,
        NULL);
    evidence.open_result.phase = LOCALCAT_PHASE_OPEN;
    if (handle == INVALID_HANDLE_VALUE) {
        evidence.open_result.status = LOCALCAT_RESULT_ERROR;
        evidence.open_result.winerror = GetLastError();
        return;
    }
    evidence.open_result.status = LOCALCAT_RESULT_OK;
    if (!CloseHandle(handle) && evidence.helper_status == LOCALCAT_STATUS_OK) {
        evidence.helper_status = LOCALCAT_STATUS_CLOSE;
        evidence.helper_winerror = GetLastError();
    }
}

static void run_write_operation(void) {
    HANDLE handle;
    LARGE_INTEGER zero;
    DWORD written = 0;
    zero.QuadPart = 0;
    evidence.write_result.phase = LOCALCAT_PHASE_OPEN;
    handle = CreateFileW(
        target_path,
        GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        NULL,
        OPEN_EXISTING,
        FILE_FLAG_OPEN_REPARSE_POINT,
        NULL);
    if (handle == INVALID_HANDLE_VALUE) {
        evidence.write_result.status = LOCALCAT_RESULT_ERROR;
        evidence.write_result.winerror = GetLastError();
        return;
    }
    evidence.write_result.phase = LOCALCAT_PHASE_SEEK;
    if (!SetFilePointerEx(handle, zero, NULL, FILE_BEGIN)) {
        evidence.write_result.status = LOCALCAT_RESULT_ERROR;
        evidence.write_result.winerror = GetLastError();
    } else {
        evidence.write_result.phase = LOCALCAT_PHASE_WRITE;
        if (!WriteFile(
                handle,
                write_payload,
                sizeof(write_payload),
                &written,
                NULL)) {
            evidence.write_result.status = LOCALCAT_RESULT_ERROR;
            evidence.write_result.winerror = GetLastError();
        } else if (written != sizeof(write_payload)) {
            evidence.write_result.status = LOCALCAT_RESULT_ERROR;
            evidence.write_result.winerror = ERROR_WRITE_FAULT;
            evidence.write_result.transferred = written;
        } else {
            evidence.write_result.transferred = written;
            evidence.write_result.phase = LOCALCAT_PHASE_SET_EOF;
            if (!SetEndOfFile(handle)) {
                evidence.write_result.status = LOCALCAT_RESULT_ERROR;
                evidence.write_result.winerror = GetLastError();
            } else {
                evidence.write_result.status = LOCALCAT_RESULT_OK;
            }
        }
    }
    if (!CloseHandle(handle) && evidence.helper_status == LOCALCAT_STATUS_OK) {
        evidence.helper_status = LOCALCAT_STATUS_CLOSE;
        evidence.helper_winerror = GetLastError();
    }
}

static void run_delete_operation(void) {
    evidence.delete_result.phase = LOCALCAT_PHASE_DELETE;
    if (!DeleteFileW(target_path)) {
        evidence.delete_result.status = LOCALCAT_RESULT_ERROR;
        evidence.delete_result.winerror = GetLastError();
        return;
    }
    evidence.delete_result.status = LOCALCAT_RESULT_OK;
}

static BOOL write_evidence(void) {
    HANDLE output = report_output;
    BYTE *cursor = (BYTE *)&evidence;
    DWORD remaining = sizeof(evidence);
    if (output == NULL || output == INVALID_HANDLE_VALUE) {
        return FALSE;
    }
    while (remaining != 0) {
        DWORD written = 0;
        if (!WriteFile(output, cursor, remaining, &written, NULL) || written == 0) {
            return FALSE;
        }
        cursor += written;
        remaining -= written;
    }
    return TRUE;
}

void WINAPI LocalCatTestEntry(void) {
    HANDLE token = NULL;
    evidence.magic = LOCALCAT_HELPER_MAGIC;
    evidence.version = LOCALCAT_HELPER_VERSION;
    evidence.record_size = sizeof(evidence);
    if (!parse_command_line()) {
        evidence.helper_status = LOCALCAT_STATUS_COMMAND_LINE;
        evidence.helper_winerror = ERROR_INVALID_PARAMETER;
    } else if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) {
        evidence.helper_status = LOCALCAT_STATUS_OPEN_TOKEN;
        evidence.helper_winerror = GetLastError();
    } else if (capture_token(token)) {
        run_read_operation();
        run_open_operation();
        run_write_operation();
        run_delete_operation();
    }
    if (token != NULL && !CloseHandle(token) && evidence.helper_status == LOCALCAT_STATUS_OK) {
        evidence.helper_status = LOCALCAT_STATUS_CLOSE;
        evidence.helper_winerror = GetLastError();
    }
    if (!write_evidence()) {
        ExitProcess(91);
    }
    ExitProcess(evidence.helper_status == LOCALCAT_STATUS_OK ? 0 : 90);
}
