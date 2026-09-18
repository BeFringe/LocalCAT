/* Interpreter-init diagnostic, not the complete E0-E11 entry gate. */
#ifdef LOCALCAT_TEST_INITIALIZATION_FAULTS
#include "localcat_frozen_bootstrap.c"
#include "windows_frozen_init_faults.h"
#else
#include "localcat_frozen_bootstrap.h"
#endif
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

struct test_api {
    PyInitConfig *(*create)(void);
    void (*release)(PyInitConfig *);
    int (*set_int)(PyInitConfig *, const char *, int64_t);
    int (*set_str)(PyInitConfig *, const char *, const char *);
    int (*set_list)(PyInitConfig *, const char *, size_t, char *const *);
    int (*initialize)(PyInitConfig *);
    int (*get_error)(PyInitConfig *, const char **);
    int (*run)(const char *, PyCompilerFlags *);
    void (*finalize)(void);
};

static int inject_pyc(const wchar_t *root, const char *input)
{
    wchar_t path[32768];
    unsigned char bytes[8192];
    FILE *source = NULL;
    HANDLE output;
    size_t count;
    DWORD written;
    if (fopen_s(&source,input,"rb") != 0 || source == NULL) return -1;
    count = fread(bytes,1,sizeof(bytes),source); fclose(source);
    if (count == 0 || count >= sizeof(bytes)) return -1;
    if (swprintf_s(path,32768,L"%ls\\_internal\\encodings\\__pycache__",root) <= 0 ||
        !CreateDirectoryW(path,NULL)) return -1;
    if (swprintf_s(path,32768,L"%ls\\_internal\\encodings\\__pycache__\\utf_8.cpython-314.pyc",root) <= 0) return -1;
    output = CreateFileW(path,GENERIC_WRITE,FILE_SHARE_READ,NULL,CREATE_NEW,0,NULL);
    if (output == INVALID_HANDLE_VALUE) return -1;
    if (!WriteFile(output,bytes,(DWORD)count,&written,NULL) || written != count) { CloseHandle(output); return -1; }
    CloseHandle(output); return 0;
}

static DWORD WINAPI wrong_thread_initialize(void *unused)
{
    const char *diagnostic = NULL;
    (void)unused;
    return localcat_bootstrap_initialize(&diagnostic) != 0 && diagnostic != NULL &&
        strcmp(diagnostic,"FROZEN_ENTRY.INITIALIZATION_STATE") == 0 ? 0U : 1U;
}

int main(int argc, char **argv)
{
    struct test_api py = {0};
    struct localcat_bundle_authority *authority;
    struct localcat_bootstrap_facts test_facts = {0};
    const char *diagnostic = NULL;
    wchar_t path[32768], source_path[32768];
    HMODULE python;
    PyInitConfig *config;
    int result;
    int full = 0;
    HANDLE original_stderr = GetStdHandle(STD_ERROR_HANDLE);
#define TEST(c) do { if (!(c)) { fprintf(stderr,"FAIL %d: %s\n",__LINE__,diagnostic ? diagnostic : #c); return 1; } } while (0)
#define RESOLVE(field,name) do { FARPROC p=GetProcAddress(python,name); TEST(p != NULL); memcpy(&py.field,&p,sizeof(p)); } while (0)
    TEST(argc == 4 && SetDefaultDllDirectories(LOAD_LIBRARY_SEARCH_SYSTEM32));
    authority = (struct localcat_bundle_authority *)calloc(1,sizeof(*authority));
    TEST(authority != NULL && localcat_bundle_authority_prepare(authority,&diagnostic) == 0);
    TEST(swprintf_s(source_path,32768,L"%ls\\_internal\\encodings\\utf_8.py",authority->root_final_path) > 0);
    full = strncmp(argv[2],"full",4) == 0;
    if (full) {
        struct localcat_retained_entry *entry = localcat_bundle_authority_find(authority,"python-runtime");
        TEST(entry != NULL);
        python = localcat_native_closure_load_verified(entry,&diagnostic);
        if (strcmp(argv[2],"full-launcher") == 0 || strcmp(argv[2],"full-empty-launcher") == 0) {
            TEST(python == NULL && diagnostic != NULL &&
                 strcmp(diagnostic,"FROZEN_ENTRY.LAUNCHER_ENVIRONMENT_REJECTED") == 0);
            TEST(GetModuleHandleW(L"python314.dll") == NULL);
            localcat_bundle_authority_close(authority); free(authority);
            puts("launcher rejected before Python load"); return 12;
        }
    } else {
        TEST(MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,argv[1],-1,path,32768) > 0);
        python = LoadLibraryExW(path,NULL,LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_SYSTEM32);
    }
    TEST(python != NULL && localcat_bootstrap_bind(python,&diagnostic) == 0);
    test_facts.abi_version = LOCALCAT_BOOTSTRAP_ABI_VERSION; test_facts.struct_size = sizeof(test_facts);
    TEST(localcat_bootstrap_configure(authority,&test_facts,&diagnostic) == 0);
#ifdef LOCALCAT_TEST_INITIALIZATION_FAULTS
    TEST(localcat_test_prepare_fault(python,argv[2]) == 0);
#endif
    RESOLVE(create,"PyInitConfig_Create"); RESOLVE(release,"PyInitConfig_Free");
    RESOLVE(set_int,"PyInitConfig_SetInt"); RESOLVE(set_str,"PyInitConfig_SetStr");
    RESOLVE(set_list,"PyInitConfig_SetStrList"); RESOLVE(initialize,"Py_InitializeFromInitConfig");
    RESOLVE(get_error,"PyInitConfig_GetError"); RESOLVE(run,"PyRun_SimpleStringFlags");
    RESOLVE(finalize,"Py_Finalize");
    if (full) {
        DWORD thread_result;
        HANDLE thread = CreateThread(NULL,0,wrong_thread_initialize,NULL,0,NULL);
        TEST(thread != NULL && WaitForSingleObject(thread,INFINITE) == WAIT_OBJECT_0);
        TEST(GetExitCodeThread(thread,&thread_result) && thread_result == 0);
        CloseHandle(thread);
        if (strcmp(argv[2],"full-no-stage-sink") == 0 || strcmp(argv[2],"full-source-failure-no-stage-sink") == 0) {
            TEST(SetStdHandle(STD_ERROR_HANDLE,INVALID_HANDLE_VALUE));
        } else if (strcmp(argv[2],"full-null-stage-sink") == 0) {
            TEST(SetStdHandle(STD_ERROR_HANDLE,NULL));
        } else if (strcmp(argv[2],"full-readonly-stage-sink") == 0) {
            struct localcat_retained_entry *entry = localcat_bundle_authority_find(authority,"fixture");
            TEST(entry != NULL && SetStdHandle(STD_ERROR_HANDLE,entry->handle));
        }
        if (strncmp(argv[2],"full-source-failure",19) == 0 || strcmp(argv[2],"full-path-layout") == 0 ||
            strcmp(argv[2],"full-native-clear") == 0 || strcmp(argv[2],"full-native-install") == 0) {
            HANDLE writable;
            TEST(localcat_bootstrap_initialize(&diagnostic) != 0);
            TEST(diagnostic != NULL && strcmp(diagnostic,strcmp(argv[2],"full-path-layout") == 0 ?
                 "FROZEN_ENTRY.INITIALIZATION_PATH_INVALID" : "FROZEN_ENTRY.INITIALIZATION_FAILED") == 0);
            TEST(localcat_bootstrap_arm(&diagnostic) != 0);
            TEST(localcat_bootstrap_execute(&diagnostic) != 0);
            TEST(localcat_bootstrap_initialize(&diagnostic) != 0);
            writable = CreateFileW(source_path,GENERIC_WRITE,FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                                   NULL,OPEN_EXISTING,0,NULL);
            TEST(writable != INVALID_HANDLE_VALUE); CloseHandle(writable);
            puts("initialization failure revoked authority"); return 13;
        }
        if (strcmp(argv[2],"full-early") == 0) {
            HANDLE writable;
            TEST(inject_pyc(authority->root_final_path,argv[3]) == 0);
            TEST(localcat_bootstrap_initialize(&diagnostic) != 0);
            TEST(diagnostic != NULL && strcmp(diagnostic,"FROZEN_ENTRY.SOURCE_DUPLICATE") == 0);
            TEST(localcat_bootstrap_arm(&diagnostic) != 0);
            TEST(localcat_bootstrap_initialize(&diagnostic) != 0);
            writable = CreateFileW(source_path,GENERIC_WRITE,FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                                   NULL,OPEN_EXISTING,0,NULL);
            TEST(writable != INVALID_HANDLE_VALUE); CloseHandle(writable);
            puts("interpreter failure closed retained handles"); return 10;
        }
        TEST(localcat_bootstrap_initialize(&diagnostic) == 0);
        TEST(strcmp(localcat_last_stage_marker(),"FROZEN_ENTRY.PATH_CONFIGURATION_BEGIN") == 0);
        TEST(SetStdHandle(STD_ERROR_HANDLE,original_stderr));
        TEST(localcat_bootstrap_initialize(&diagnostic) != 0);
        diagnostic = NULL;
    } else {
    config = py.create(); TEST(config != NULL);
    TEST(py.set_int(config,"isolated",1) == 0);
    TEST(py.set_int(config,"use_environment",0) == 0);
    TEST(py.set_int(config,"site_import",0) == 0);
    TEST(py.set_int(config,"write_bytecode",0) == 0);
    TEST(py.set_int(config,"safe_path",1) == 0);
    TEST(py.set_int(config,"parse_argv",0) == 0);
    TEST(py.set_int(config,"user_site_directory",0) == 0);
    TEST(py.set_str(config,"stdio_encoding","utf-8") == 0);
    TEST(py.set_list(config,"module_search_paths",0,NULL) == 0);
    TEST(py.set_int(config,"module_search_paths_set",1) == 0);
    TEST(localcat_bootstrap_register(config,&diagnostic) == 0);
    TEST(localcat_bootstrap_register_sources(config,&diagnostic) == 0);
    TEST(localcat_bootstrap_register_sources(config,&diagnostic) != 0);
    diagnostic = NULL;
    if (strcmp(argv[2],"early") == 0) TEST(inject_pyc(authority->root_final_path,argv[3]) == 0);
    result = py.initialize(config);
    if (result != 0) {
        HANDLE writable;
        py.get_error(config,&diagnostic);
        fprintf(stderr,"native marker: %s\n",localcat_bootstrap_last_diagnostic() ? localcat_bootstrap_last_diagnostic() : "none");
        if (strcmp(argv[2],"early") != 0) {
            fprintf(stderr,"FAIL initialization: %s\n",diagnostic ? diagnostic : "unknown"); return 1;
        }
        writable = CreateFileW(source_path,GENERIC_WRITE,FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                               NULL,OPEN_EXISTING,0,NULL);
        TEST(writable != INVALID_HANDLE_VALUE);
        CloseHandle(writable); py.release(config); localcat_bootstrap_abort();
        puts("interpreter failure closed retained handles"); return 10;
    }
    TEST(result == 0); py.release(config);
    }
    if (strcmp(argv[2],"late") == 0) TEST(inject_pyc(authority->root_final_path,argv[3]) == 0);
    if (full) TEST(py.run("import sys, _frozen_importlib_external as external, _frozen_importlib as core\n"
                         "assert external is core._bootstrap_external\n"
                         "assert external.__loader__.__name__ == 'BuiltinImporter'\n"
                         "assert external._install.__code__.co_filename == '<localcat-retained:importlib-external>'\n"
                         "assert sum(item is external.PathFinder for item in sys.meta_path) == 1\n"
                         "assert sys.executable.endswith('retained-init.exe'), sys.executable\n",NULL) == 0);
    TEST(py.run("import sys, encodings, encodings.utf_8\n"
                "assert sys.path == [], sys.path\n"
                "assert sys.flags.isolated == 1\n"
                "assert sys.flags.ignore_environment == 1\n"
                "assert sys.flags.no_user_site == 1\n"
                "assert sys.flags.no_site == 1 and 'site' not in sys.modules\n"
                "assert sys.flags.safe_path is True\n"
                "assert sys.flags.dont_write_bytecode == 1\n"
                "assert sys._xoptions == {} and sys.warnoptions == []\n"
                "assert 'faulthandler' not in sys.modules and 'tracemalloc' not in sys.modules\n"
                "assert 'ambient-launcher' not in sys.executable, 'ambient executable override'\n"
                "assert encodings.__loader__.__name__ == 'BuiltinImporter'\n"
                "assert encodings.utf_8.__loader__.__name__ == 'BuiltinImporter'\n"
                "assert 'verified'.encode('utf-8') == b'verified'\n"
                "del sys.modules['encodings.utf_8']\n"
                "import encodings.utf_8\n"
                "assert encodings.utf_8.__loader__.__name__ == 'BuiltinImporter'\n",NULL) == 0);
    if (strcmp(argv[2],"full-badpath") == 0 || strcmp(argv[2],"full-badflags") == 0) {
        HANDLE writable;
        TEST(py.run(strcmp(argv[2],"full-badpath") == 0 ? "sys.path.append('ambient')" :
            "class ChangedFlags:\n isolated=0\nsys.flags=ChangedFlags()\n",NULL) == 0);
        TEST(localcat_bootstrap_arm(&diagnostic) != 0);
        TEST(diagnostic != NULL && strcmp(diagnostic,"FROZEN_ENTRY.ISOLATION_MISMATCH") == 0);
        TEST(localcat_bootstrap_execute(&diagnostic) != 0);
        writable = CreateFileW(source_path,GENERIC_WRITE,FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                               NULL,OPEN_EXISTING,0,NULL);
        TEST(writable != INVALID_HANDLE_VALUE); CloseHandle(writable);
        py.finalize(); puts("isolation mismatch revoked authority"); return 11;
    }
    if (strcmp(argv[2],"late") == 0) {
        HANDLE writable;
        TEST(localcat_bootstrap_arm(&diagnostic) != 0);
        TEST(diagnostic != NULL && strcmp(diagnostic,"FROZEN_ENTRY.SOURCE_DUPLICATE") == 0);
        TEST(localcat_bootstrap_execute(&diagnostic) != 0);
        writable = CreateFileW(source_path,GENERIC_WRITE,FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                               NULL,OPEN_EXISTING,0,NULL);
        TEST(writable != INVALID_HANDLE_VALUE); CloseHandle(writable);
        TEST(localcat_bootstrap_arm(&diagnostic) != 0);
    } else {
        TEST(localcat_bootstrap_arm(&diagnostic) == 0);
    }
    if (full) {
        TEST(localcat_bootstrap_execute(&diagnostic) == 0);
        TEST(py.run("import localcat_spike_bootstrap as boot\n"
                    "assert boot._critical['verify_fixture'](boot._fixture)\n"
                    "assert boot._loader.attestation[0] == 'critical-source'\n"
                    "assert boot.__loader__ is None\n"
                    "assert boot.__spec__.origin == boot.__file__ == '_internal/localcat_frozen_bootstrap.py'\n"
                    "assert boot._bootstrap_attestation[1] == 3\n",NULL) == 0);
        TEST(localcat_bootstrap_execute(&diagnostic) != 0);
    }
    py.finalize(); localcat_bootstrap_abort();
    puts("retained interpreter initialization: PASS"); return 0;
}
