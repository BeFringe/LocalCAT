/* Test translation unit only. Production entry never includes this file. */
#ifndef LOCALCAT_TEST_INITIALIZATION_FAULTS
#error Fault helpers must not be compiled into a production entry
#endif

static PyObject *(*test_getattr)(PyObject *, const char *);
static PyObject *(*test_eval)(PyObject *, PyObject *, PyObject *);
static void (*test_set_path)(const wchar_t *);
static void *(*test_raw_malloc)(size_t);
static int (*test_initialize)(PyInitConfig *);
static HANDLE test_memory_job;
static unsigned int test_source_evaluations;
static int test_install_fault;

__declspec(dllexport) __declspec(noinline) void localcat_fault_probe_begin(void)
{
    volatile int marker = 1; (void)marker;
}

__declspec(dllexport) __declspec(noinline) void localcat_fault_probe_returned(int external_body, int init_result)
{
    volatile int marker = external_body + init_result; (void)marker;
}

static void test_add_traceback(void)
{
    static const char source[] = "import sys\nsys._localcat_traceback_body_executed = True\n";
    wchar_t path[32768];
    HANDLE file;
    DWORD written;
    if (bundle == NULL || swprintf_s(path,32768,L"%ls\\traceback.py",bundle->root_final_path) <= 0) ExitProcess(95);
    file = CreateFileW(path,GENERIC_WRITE,FILE_SHARE_READ,NULL,CREATE_NEW,0,NULL);
    if (file == INVALID_HANDLE_VALUE) ExitProcess(96);
    if (!WriteFile(file,source,sizeof(source)-1U,&written,NULL) || written != sizeof(source)-1U) ExitProcess(97);
    CloseHandle(file);
    localcat_stage_marker("TEST_ONLY.TRACEBACK_CREATED_AFTER_PROOF");
}

static int test_observe_initialization(PyInitConfig *config)
{
    int result;
    localcat_fault_probe_begin();
    result = test_initialize(config);
    localcat_fault_probe_returned(api.PySys_GetObject("_localcat_traceback_body_executed") != NULL,result);
    localcat_stage_marker(api.PySys_GetObject("_localcat_traceback_body_executed") == NULL ?
                          "TEST_ONLY.EXTERNAL_BODY_MARKER=0" : "TEST_ONLY.EXTERNAL_BODY_MARKER=1");
    return result;
}

static PyObject *test_observe_eval(PyObject *code, PyObject *globals, PyObject *locals)
{
    if (test_install_fault && test_source_evaluations == 0U) test_add_traceback();
    ++test_source_evaluations;
    return test_eval(code,globals,locals);
}

static PyObject *test_fail_clear(PyObject *object, const char *name)
{
    if (object == api.PySys_GetObject("path") && strcmp(name,"clear") == 0) {
        if (test_source_evaluations != 0U || interpreter_path_cleared) {
            localcat_stage_marker("TEST_ONLY.CLEAR_FAULT_TOO_LATE");
            ExitProcess(91);
        }
        localcat_stage_marker("TEST_ONLY.CLEAR_FAILED_BEFORE_SOURCE");
        test_add_traceback();
        api.PyErr_SetString(*api.runtime_error,"TEST_ONLY.CLEAR_OPERATION_FAILED");
        return NULL;
    }
    return test_getattr(object,name);
}

static void test_exhaust_then_set_path(const wchar_t *path)
{
    unsigned long allocations = 0;
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits = {0};
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_PROCESS_MEMORY;
    limits.ProcessMemoryLimit = 64U * 1024U * 1024U;
    if (!SetInformationJobObject(test_memory_job,JobObjectExtendedLimitInformation,&limits,sizeof(limits)) ||
        !AssignProcessToJobObject(test_memory_job,GetCurrentProcess())) {
        localcat_stage_marker("TEST_ONLY.JOB_LIMIT_UNAVAILABLE"); ExitProcess(92);
    }
    localcat_stage_marker("TEST_ONLY.JOB_MEMORY_LIMIT_64_MIB");
    /* Deliberately retain allocations only in this bounded disposable child.
     * This is the DLL's real raw allocator, not an overwritten DLL function,
     * fake Py_SetPath return or replaceable PyMem allocator callback. */
    while (test_raw_malloc(sizeof(wchar_t)) != NULL) {
        if (++allocations >= 8000000UL) {
            localcat_stage_marker("TEST_ONLY.EXHAUSTION_NOT_REACHED"); ExitProcess(93);
        }
    }
    localcat_stage_marker("TEST_ONLY.RAW_ALLOCATOR_EXHAUSTED");
    test_set_path(path);
    localcat_stage_marker("TEST_ONLY.PATH_CALL_RETURNED");
    ExitProcess(94);
}

static int localcat_test_prepare_fault(HMODULE python, const char *mode)
{
    SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX);
    if (strcmp(mode,"full-native-clear") == 0 || strcmp(mode,"full-native-install") == 0) {
        test_getattr = api.PyObject_GetAttrString;
        test_eval = api.PyEval_EvalCode;
        test_install_fault = strcmp(mode,"full-native-install") == 0;
        if (!test_install_fault) api.PyObject_GetAttrString = test_fail_clear;
        api.PyEval_EvalCode = test_observe_eval;
        test_initialize = api.Py_InitializeFromInitConfig;
        api.Py_InitializeFromInitConfig = test_observe_initialization;
        return 0;
    }
    if (strcmp(mode,"full-native-oom") == 0) {
        FARPROC raw_malloc = GetProcAddress(python,"PyMem_RawMalloc");
        if (raw_malloc == NULL) return -1;
        memcpy(&test_raw_malloc,&raw_malloc,sizeof(raw_malloc));
        test_memory_job = CreateJobObjectW(NULL,NULL);
        if (test_memory_job == NULL) return -1;
        test_set_path = api.Py_SetPath;
        api.Py_SetPath = test_exhaust_then_set_path;
        return 0;
    }
    return -1;
}
