/* PEP 741 diagnostic harness: ordinary trusted Python, not a frozen gate. */
#include "localcat_frozen_bootstrap.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

struct test_api {
    PyInitConfig *(*create)(void);
    void (*release)(PyInitConfig *);
    int (*set_int)(PyInitConfig *, const char *, int64_t);
    int (*set_str)(PyInitConfig *, const char *, const char *);
    int (*initialize)(PyInitConfig *);
    int (*run)(const char *, PyCompilerFlags *);
    void (*finalize)(void);
};

int main(int argc, char **argv)
{
    struct localcat_bundle_authority *authority;
    struct localcat_retained_entry *native;
    struct localcat_bootstrap_facts binding = {0};
    struct test_api py = {0};
    const char *diagnostic = NULL;
    char path[32768];
    wchar_t wide[32768];
    wchar_t fixture_path[32768];
    HANDLE writable;
    HMODULE python;
    PyInitConfig *config;
    FILE *script;
    long length;
    char *program;
    int result;
#define TEST(condition) do { if (!(condition)) { fprintf(stderr, "FAIL line %d: %s\n", __LINE__, diagnostic ? diagnostic : #condition); return 1; } } while (0)
#define RESOLVE(field, symbol) do { FARPROC value = GetProcAddress(python, symbol); TEST(value != NULL); memcpy(&py.field, &value, sizeof(value)); } while (0)
    TEST(argc == 4);
    TEST(SetDefaultDllDirectories(LOAD_LIBRARY_SEARCH_SYSTEM32));
    authority = (struct localcat_bundle_authority *)calloc(1, sizeof(*authority));
    TEST(authority != NULL);
    TEST(localcat_bundle_authority_prepare(authority, &diagnostic) == 0);
    TEST(swprintf_s(fixture_path, 32768, L"%ls\\_internal\\fixture.txt", authority->root_final_path) > 0);
    native = localcat_bundle_authority_find(authority, "native-probe");
    TEST(native != NULL && localcat_native_closure_load_verified(native, &diagnostic) != NULL);
    sprintf_s(path, sizeof(path), "%s\\python314.dll", argv[1]);
    TEST(MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, path, -1, wide, 32768) > 0);
    python = LoadLibraryExW(wide, NULL, LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_SYSTEM32);
    TEST(python != NULL && localcat_bootstrap_bind(python, &diagnostic) == 0);
    binding.abi_version = LOCALCAT_BOOTSTRAP_ABI_VERSION + 1U;
    binding.struct_size = sizeof(binding);
    memset(binding.build_id, 1, 32); memset(binding.candidate_digest, 2, 32);
    memset(binding.prelink_digest, 3, 32); memset(binding.trace_digest, 4, 32);
    TEST(localcat_bootstrap_configure(authority, &binding, &diagnostic) != 0);
    binding.abi_version = LOCALCAT_BOOTSTRAP_ABI_VERSION;
    binding.struct_size--;
    TEST(localcat_bootstrap_configure(authority, &binding, &diagnostic) != 0);
    binding.struct_size = sizeof(binding);
    TEST(localcat_bootstrap_configure(authority, &binding, &diagnostic) == 0);
    TEST(localcat_bootstrap_configure(authority, &binding, &diagnostic) != 0);
    TEST(localcat_bootstrap_arm(&diagnostic) != 0);
    TEST(localcat_bootstrap_seal_trace(binding.trace_digest, &diagnostic) != 0);
    RESOLVE(create, "PyInitConfig_Create"); RESOLVE(release, "PyInitConfig_Free");
    RESOLVE(set_int, "PyInitConfig_SetInt"); RESOLVE(set_str, "PyInitConfig_SetStr");
    RESOLVE(initialize, "Py_InitializeFromInitConfig"); RESOLVE(run, "PyRun_SimpleStringFlags");
    RESOLVE(finalize, "Py_Finalize");
    config = py.create(); TEST(config != NULL);
    TEST(py.set_int(config, "isolated", 1) == 0);
    TEST(py.set_int(config, "use_environment", 0) == 0);
    TEST(py.set_int(config, "site_import", 0) == 0);
    TEST(py.set_int(config, "write_bytecode", 0) == 0);
    TEST(py.set_str(config, "home", argv[1]) == 0);
    TEST(localcat_bootstrap_register(config, &diagnostic) == 0);
    TEST(localcat_bootstrap_register(config, &diagnostic) != 0);
    TEST(py.initialize(config) == 0); py.release(config);
    TEST(py.run("try:\n import _localcat_frozen_bootstrap\nexcept RuntimeError:\n pass\nelse:\n raise AssertionError('pre-arm import accepted')\n", NULL) == 0);
    if (strcmp(argv[3],"native-proof-failure") == 0) {
        native->actual_module_reproved = 0U;
        TEST(localcat_bootstrap_arm(&diagnostic) != 0);
        /* The failed owner is freed: never write back through the old entry
         * pointer to manufacture a recoverable proof or a use-after-free. */
        TEST(localcat_bootstrap_arm(&diagnostic) != 0);
        TEST(localcat_bootstrap_execute(&diagnostic) != 0);
        writable = CreateFileW(fixture_path,GENERIC_WRITE,FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                               NULL,OPEN_EXISTING,0,NULL);
        TEST(writable != INVALID_HANDLE_VALUE); CloseHandle(writable);
        py.finalize(); localcat_bootstrap_abort();
        puts("native proof failure revoked authority: PASS"); return 0;
    }
    TEST(localcat_bootstrap_arm(&diagnostic) == 0);
    TEST(localcat_bootstrap_arm(&diagnostic) != 0);
    memset(binding.trace_digest, 5, 32);
    TEST(localcat_bootstrap_seal_trace(NULL, &diagnostic) != 0);
    TEST(localcat_bootstrap_seal_trace(binding.trace_digest, &diagnostic) == 0);
    memset(binding.trace_digest, 6, 32);
    TEST(localcat_bootstrap_seal_trace(binding.trace_digest, &diagnostic) != 0);
    TEST(fopen_s(&script, argv[2], "rb") == 0);
    TEST(fseek(script, 0, SEEK_END) == 0); length = ftell(script);
    TEST(length >= 0 && length < 1024 * 1024 && fseek(script, 0, SEEK_SET) == 0);
    program = (char *)malloc((size_t)length + 1U); TEST(program != NULL);
    TEST(fread(program, 1, (size_t)length, script) == (size_t)length); fclose(script);
    program[length] = 0; result = py.run(program, NULL); free(program);
    TEST(result == 0);
    writable = CreateFileW(fixture_path, GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        NULL, OPEN_EXISTING, 0, NULL);
    TEST(writable != INVALID_HANDLE_VALUE);
    CloseHandle(writable);
    py.finalize();
    localcat_bootstrap_abort(); localcat_bootstrap_abort();
    puts("native handoff diagnostic: PASS");
    return 0;
}
