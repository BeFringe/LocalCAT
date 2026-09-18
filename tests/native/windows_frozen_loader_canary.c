/* Validation input only. No product export, CRT, file, network or thread work. */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#ifdef LOCALCAT_CANARY_DLL
BOOL WINAPI DllMain(HINSTANCE module, DWORD reason, LPVOID reserved)
{
    (void)module;
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        OutputDebugStringW(L"LOCALCAT_DLL_CANARY_PROCESS_ATTACH\n");
    }
    return TRUE;
}
#else
void WINAPI LocalCatCanaryControl(void)
{
    HMODULE module = LoadLibraryExW(L"localcat-loader-canary.dll", NULL,
        LOAD_LIBRARY_SEARCH_APPLICATION_DIR | LOAD_LIBRARY_SEARCH_SYSTEM32);
    ExitProcess(module != NULL ? 0U : 1U);
}
#endif
