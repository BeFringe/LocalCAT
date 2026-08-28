/* Diagnostic-only Task 1.6 call-surface probe; never linked into the product. */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <stdint.h>

typedef struct _UNICODE_STRING {
    USHORT Length;
    USHORT MaximumLength;
    PWSTR Buffer;
} UNICODE_STRING;

typedef struct _LDR_DLL_LOADED_NOTIFICATION_DATA {
    ULONG Flags;
    const UNICODE_STRING *FullDllName;
    const UNICODE_STRING *BaseDllName;
    PVOID DllBase;
    ULONG SizeOfImage;
} LDR_DLL_LOADED_NOTIFICATION_DATA;

typedef union _LDR_DLL_NOTIFICATION_DATA {
    LDR_DLL_LOADED_NOTIFICATION_DATA Loaded;
    LDR_DLL_LOADED_NOTIFICATION_DATA Unloaded;
} LDR_DLL_NOTIFICATION_DATA;

typedef VOID (CALLBACK *LDR_DLL_NOTIFICATION_FUNCTION)(ULONG, const LDR_DLL_NOTIFICATION_DATA *, PVOID);
typedef LONG (NTAPI *LdrRegisterDllNotificationFn)(ULONG, LDR_DLL_NOTIFICATION_FUNCTION, PVOID, PVOID *);
typedef struct _PyInitConfig PyInitConfig;
typedef PyInitConfig *(*PyInitConfig_CreateFn)(void);
typedef void (*PyInitConfig_FreeFn)(PyInitConfig *);
typedef int (*PyInitConfig_SetIntFn)(PyInitConfig *, const char *, int64_t);
typedef int (*PyInitConfig_SetStrFn)(PyInitConfig *, const char *, const char *);
typedef int (*PyInitConfig_SetStrListFn)(PyInitConfig *, const char *, size_t, char * const *);
typedef int (*Py_InitializeFromInitConfigFn)(PyInitConfig *);
typedef int (*Py_FinalizeFn)(void);

static volatile LONG probe_stage = 0;
struct probe_record {
    LONG stage;
    USHORT base_name_length;
    USHORT full_name_length;
    wchar_t base_name[128];
    wchar_t full_name[1024];
};
static struct probe_record probe_records[128];
static volatile LONG probe_record_count = 0;

struct module_record {
    const wchar_t *role;
    wchar_t path[32768];
    DWORD volume_serial;
    DWORD file_index_high;
    DWORD file_index_low;
    DWORD file_size_high;
    DWORD file_size_low;
};

static VOID CALLBACK
probe_notification(ULONG reason, const LDR_DLL_NOTIFICATION_DATA *data, PVOID context)
{
    LONG slot;
    USHORT base_name_length;
    USHORT full_name_length;
    (void)context;
    if (reason == 1 && data && data->Loaded.BaseDllName) {
        slot = InterlockedIncrement(&probe_record_count) - 1;
        if (slot < 0 || slot >= (LONG)(sizeof(probe_records) / sizeof(probe_records[0]))) {
            return;
        }
        base_name_length = data->Loaded.BaseDllName->Length / sizeof(wchar_t);
        if (base_name_length >= (USHORT)(sizeof(probe_records[slot].base_name) / sizeof(wchar_t))) {
            base_name_length = (USHORT)(sizeof(probe_records[slot].base_name) / sizeof(wchar_t) - 1);
        }
        probe_records[slot].stage = probe_stage;
        probe_records[slot].base_name_length = base_name_length;
        CopyMemory(probe_records[slot].base_name, data->Loaded.BaseDllName->Buffer,
            base_name_length * sizeof(wchar_t));
        probe_records[slot].base_name[base_name_length] = 0;
        if (!data->Loaded.FullDllName) {
            return;
        }
        full_name_length = data->Loaded.FullDllName->Length / sizeof(wchar_t);
        if (full_name_length >= (USHORT)(sizeof(probe_records[slot].full_name) / sizeof(wchar_t))) {
            full_name_length = (USHORT)(sizeof(probe_records[slot].full_name) / sizeof(wchar_t) - 1);
        }
        probe_records[slot].full_name_length = full_name_length;
        CopyMemory(probe_records[slot].full_name, data->Loaded.FullDllName->Buffer,
            full_name_length * sizeof(wchar_t));
        probe_records[slot].full_name[full_name_length] = 0;
    }
}

static void
probe_dump_records(void)
{
    LONG index;
    LONG count = probe_record_count;
    if (count > (LONG)(sizeof(probe_records) / sizeof(probe_records[0]))) {
        count = (LONG)(sizeof(probe_records) / sizeof(probe_records[0]));
    }
    for (index = 0; index < count; ++index) {
        fwprintf(stderr, L"TRACE stage=%ld load=%.*ls path=%.*ls\n", probe_records[index].stage,
            (int)probe_records[index].base_name_length, probe_records[index].base_name,
            (int)probe_records[index].full_name_length, probe_records[index].full_name);
    }
    fflush(stderr);
}

static int
probe_record_module(HMODULE module, const wchar_t *role, struct module_record *record)
{
    DWORD length;
    HANDLE file;
    BY_HANDLE_FILE_INFORMATION information;
    record->role = role;
    length = GetModuleFileNameW(module, record->path,
        (DWORD)(sizeof(record->path) / sizeof(record->path[0])));
    if (length == 0 || length >= (DWORD)(sizeof(record->path) / sizeof(record->path[0]))) {
        return 0;
    }
    file = CreateFileW(record->path, FILE_READ_ATTRIBUTES,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, NULL, OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT, NULL);
    if (file == INVALID_HANDLE_VALUE) {
        return 0;
    }
    if (!GetFileInformationByHandle(file, &information)) {
        CloseHandle(file);
        return 0;
    }
    CloseHandle(file);
    record->volume_serial = information.dwVolumeSerialNumber;
    record->file_index_high = information.nFileIndexHigh;
    record->file_index_low = information.nFileIndexLow;
    record->file_size_high = information.nFileSizeHigh;
    record->file_size_low = information.nFileSizeLow;
    return 1;
}

static void
probe_dump_module(const struct module_record *record)
{
    fwprintf(stderr,
        L"MODULE role=%ls path=%ls volume=%08lX file_id=%08lX%08lX size=%08lX%08lX\n",
        record->role, record->path, record->volume_serial, record->file_index_high,
        record->file_index_low, record->file_size_high, record->file_size_low);
}

#define BIND(module, name, type) type name = (type)GetProcAddress((module), #name)

int wmain(int argc, wchar_t **argv)
{
    HMODULE ntdll, runtime, python;
    LdrRegisterDllNotificationFn register_notification;
    PVOID cookie = NULL;
    PyInitConfig *config;
    char home_utf8[32768];
    char lib_utf8[32768];
    char *paths[1];
    wchar_t runtime_path[32768];
    wchar_t python_path[32768];
    struct module_record runtime_record;
    struct module_record python_record;
    if (argc != 2) {
        fputs("usage: probe <CPython-root>\n", stderr);
        return 64;
    }
    if (!SetDefaultDllDirectories(LOAD_LIBRARY_SEARCH_SYSTEM32)) {
        return 65;
    }
    ntdll = GetModuleHandleW(L"ntdll.dll");
    register_notification = (LdrRegisterDllNotificationFn)GetProcAddress(ntdll, "LdrRegisterDllNotification");
    if (!register_notification || register_notification(0, probe_notification, NULL, &cookie) != 0) {
        return 66;
    }
    if (swprintf_s(runtime_path, 32768, L"%ls\\vcruntime140.dll", argv[1]) < 0 ||
        swprintf_s(python_path, 32768, L"%ls\\python314.dll", argv[1]) < 0) {
        return 75;
    }
    probe_stage = 1;
    runtime = LoadLibraryExW(runtime_path, NULL,
        LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_SYSTEM32);
    if (!runtime) { return 67; }
    probe_stage = 2;
    python = LoadLibraryExW(python_path, NULL,
        LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_SYSTEM32);
    if (!python) { return 68; }
    if (!probe_record_module(runtime, L"vcruntime", &runtime_record) ||
        !probe_record_module(python, L"python", &python_record)) { return 76; }
    BIND(python, PyInitConfig_Create, PyInitConfig_CreateFn);
    BIND(python, PyInitConfig_Free, PyInitConfig_FreeFn);
    BIND(python, PyInitConfig_SetInt, PyInitConfig_SetIntFn);
    BIND(python, PyInitConfig_SetStr, PyInitConfig_SetStrFn);
    BIND(python, PyInitConfig_SetStrList, PyInitConfig_SetStrListFn);
    BIND(python, Py_InitializeFromInitConfig, Py_InitializeFromInitConfigFn);
    BIND(python, Py_Finalize, Py_FinalizeFn);
    if (!PyInitConfig_Create || !PyInitConfig_Free || !PyInitConfig_SetInt || !PyInitConfig_SetStr ||
        !PyInitConfig_SetStrList || !Py_InitializeFromInitConfig || !Py_Finalize) { return 69; }
    if (!WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, argv[1], -1, home_utf8, sizeof(home_utf8), NULL, NULL)) { return 70; }
    if (sprintf_s(lib_utf8, sizeof(lib_utf8), "%s\\Lib", home_utf8) < 0) { return 71; }
    paths[0] = lib_utf8;
    config = PyInitConfig_Create();
    if (!config) { return 72; }
    if (PyInitConfig_SetInt(config, "use_environment", 0) < 0 ||
        PyInitConfig_SetInt(config, "user_site_directory", 0) < 0 ||
        PyInitConfig_SetInt(config, "site_import", 0) < 0 ||
        PyInitConfig_SetInt(config, "write_bytecode", 0) < 0 ||
        PyInitConfig_SetInt(config, "safe_path", 1) < 0 ||
        PyInitConfig_SetInt(config, "parse_argv", 0) < 0 ||
        PyInitConfig_SetStr(config, "home", home_utf8) < 0 ||
        PyInitConfig_SetStrList(config, "module_search_paths", 1, paths) < 0) { return 73; }
    probe_stage = 3;
    if (Py_InitializeFromInitConfig(config) < 0) { probe_dump_records(); return 74; }
    probe_stage = 4;
    PyInitConfig_Free(config);
    probe_dump_records();
    probe_dump_module(&runtime_record);
    probe_dump_module(&python_record);
    fflush(stderr);
    Py_Finalize();
    return 0;
}
