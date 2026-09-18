/* Diagnostic-only replay, NOT product startup, E4 proof, or an authorization gate.
 * CRT executes before wmain: SetDefaultDllDirectories here makes no E0 claim.
 * Queries have no atomic binding to a later NULL-handle system RNG call.
 * The first mode query is a candidate-bound private x64 ABI. The fallback's
 * PROCESS_EXTENDED_BASIC_INFORMATION / IsSecureProcess bit is documented by
 * Microsoft; this user-mode replay still interprets only observed responses.
 * Do not reuse it as a run gate or a complete model of all binary branches.
 * No RNG call, algorithm open, explicit provider load, or registry write occurs.
 *
 * Documented query contracts (not private-function names or changing RVAs):
 * https://learn.microsoft.com/en-us/windows/win32/api/bcrypt/nf-bcrypt-bcryptresolveproviders
 * https://learn.microsoft.com/en-us/windows/win32/api/bcrypt/nf-bcrypt-bcryptqueryproviderregistration
 * https://learn.microsoft.com/en-us/windows/win32/api/winternl/nf-winternl-ntqueryinformationprocess
 * https://learn.microsoft.com/en-us/windows/win32/procthread/isolated-user-mode--ium--processes
 * https://learn.microsoft.com/en-us/windows/win32/api/libloaderapi/nf-libloaderapi-loadlibraryexw
 * Build: MSVC 14.44.35207 x64, SDK 10.0.26100.0; /W4 /WX /TC, kernel32.lib.
 * bcrypt is dynamically bound; neither it nor a provider is a linker input.
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <bcrypt.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifndef _WIN64
#error This diagnostic reproduces only the observed x64 ABI.
#endif

#define MAX_ITEMS 512UL
static size_t total_items = 0, encoded_bytes = 0;
typedef NTSTATUS (NTAPI *QueryProcess)(HANDLE, ULONG, PVOID, ULONG, PULONG);
typedef NTSTATUS (WINAPI *ResolveProviders)(LPCWSTR, ULONG, LPCWSTR, LPCWSTR,
                                           ULONG, ULONG, ULONG *, PCRYPT_PROVIDER_REFS *);
typedef NTSTATUS (WINAPI *QueryRegistration)(LPCWSTR, ULONG, ULONG, ULONG *, PCRYPT_PROVIDER_REG *);
typedef VOID (WINAPI *FreeBuffer)(PVOID);

typedef struct ModeObservation {
    NTSTATUS status86;
    NTSTATUS status0;
    ULONG64 buffer86[22]; /* Exactly 0xb0 bytes, aligned and zero-initialized. */
    ULONG64 buffer0[8];   /* Exactly 0x40 bytes; first pointer-sized slot = size. */
    BOOL queried0;
    int derived;
} ModeObservation;

static void fail(const char *message) {
    fprintf(stderr, "diagnostic error: %s (win32=%lu)\n", message, GetLastError());
    exit(2);
}

static void checked_count(ULONG count, const void *pointer) {
    if (count > MAX_ITEMS || (count && !pointer)) fail("invalid or oversized query array");
    total_items += count;
    if (total_items > 4096) fail("aggregate query array limit exceeded");
}

static void encoded_budget(size_t size) {
    encoded_bytes += size;
    if (encoded_bytes > 2 * 1024 * 1024) fail("aggregate diagnostic size limit exceeded");
}

static void string_json(LPCWSTR value) {
    size_t i;
    if (!value) fail("unexpected null string");
    putchar('"');
    for (i = 0; value[i]; ++i) {
        unsigned int ch = (unsigned int)value[i];
        if (i >= 32768) fail("oversized string");
        encoded_budget(6); /* Maximum JSON expansion of one UTF-16 code unit. */
        if (ch == '"' || ch == '\\') { putchar('\\'); putchar((int)ch); }
        else if (ch < 32 || ch >= 127) printf("\\u%04x", ch);
        else putchar((int)ch);
    }
    putchar('"');
}

static void bytes_json(const void *value, size_t size) {
    const unsigned char *bytes = (const unsigned char *)value;
    size_t i;
    if (size > 65536 || (size && !value)) fail("invalid or oversized property");
    encoded_budget(size * 2);
    putchar('"');
    for (i = 0; i < size; ++i) printf("%02x", bytes[i]);
    putchar('"');
}

static void observe_mode(QueryProcess query, ModeObservation *out) {
    ULONG flags;
    ZeroMemory(out, sizeof(*out));
    out->derived = -1;
    /* Both pinned DLLs compute EDX as R9(0xb0) - 0x5a = 0x56, not 0x5a. */
    out->status86 = query(GetCurrentProcess(), 0x56, out->buffer86,
                          (ULONG)sizeof(out->buffer86), NULL);
    /* The DLLs fall back on ANY negative NTSTATUS. This diagnostic interprets
     * only the actually observed INVALID_INFO_CLASS case; others remain raw. */
    if ((ULONG)out->status86 != 0xc0000003UL) return;
    out->queried0 = TRUE;
    out->buffer0[0] = sizeof(out->buffer0);
    out->status0 = query(GetCurrentProcess(), 0, out->buffer0,
                         (ULONG)sizeof(out->buffer0), NULL);
    if (out->status0 != 0) return;
    memcpy(&flags, (const unsigned char *)out->buffer0 + 0x38, sizeof(flags));
    out->derived = (flags & 0x80) ? 4 : 1;
}

static void mode_json(const ModeObservation *mode) {
    printf("{\"query86_status\":%lu,\"query86_buffer_hex\":", (ULONG)mode->status86);
    bytes_json(mode->buffer86, sizeof(mode->buffer86));
    printf(",\"query0_status\":");
    if (mode->queried0) printf("%lu", (ULONG)mode->status0); else printf("null");
    printf(",\"query0_buffer_hex\":");
    if (mode->queried0) bytes_json(mode->buffer0, sizeof(mode->buffer0)); else printf("null");
    printf(",\"derived_mode\":");
    if (mode->derived < 0) printf("null"); else printf("%d", mode->derived);
    putchar('}');
}

static void image_ref_json(PCRYPT_IMAGE_REF image) {
    if (!image) { printf("null"); return; }
    printf("{\"image\":"); string_json(image->pszImage);
    printf(",\"flags\":%lu}", image->dwFlags);
}

static void image_reg_json(PCRYPT_IMAGE_REG image) {
    ULONG i, j;
    if (!image) { printf("null"); return; }
    checked_count(image->cInterfaces, image->rgpInterfaces);
    printf("{\"image\":"); string_json(image->pszImage); printf(",\"interfaces\":[");
    for (i = 0; i < image->cInterfaces; ++i) {
        PCRYPT_INTERFACE_REG item = image->rgpInterfaces[i];
        if (!item) fail("null registered interface");
        checked_count(item->cFunctions, item->rgpszFunctions);
        if (i) putchar(',');
        printf("{\"interface\":%lu,\"flags\":%lu,\"functions\":[", item->dwInterface, item->dwFlags);
        for (j = 0; j < item->cFunctions; ++j) {
            if (j) putchar(',');
            string_json(item->rgpszFunctions[j]);
        }
        printf("]}");
    }
    printf("]}");
}

static void registration_json(NTSTATUS status, PCRYPT_PROVIDER_REG reg) {
    ULONG i;
    printf("{\"status\":%lu,\"aliases\":[", (ULONG)status);
    if (status == 0) {
        if (!reg) fail("successful registration query returned null");
        checked_count(reg->cAliases, reg->rgpszAliases);
        for (i = 0; i < reg->cAliases; ++i) {
            if (i) putchar(',');
            string_json(reg->rgpszAliases[i]);
        }
    }
    printf("],\"um\":"); image_reg_json(status == 0 ? reg->pUM : NULL);
    printf(",\"km\":"); image_reg_json(status == 0 ? reg->pKM : NULL);
    putchar('}');
}

/* Emit the common observation fields, leaving the object open for registration. */
static void provider_json(PCRYPT_PROVIDER_REF provider) {
    ULONG j;
    if (!provider) fail("null provider record");
    printf("{\"name\":"); string_json(provider->pszProvider);
    printf(",\"interface\":%lu,\"function\":", provider->dwInterface); string_json(provider->pszFunction);
    printf(",\"properties\":[");
    checked_count(provider->cProperties, provider->rgpProperties);
    for (j = 0; j < provider->cProperties; ++j) {
        PCRYPT_PROPERTY_REF property = provider->rgpProperties[j];
        if (!property) fail("null property record");
        if (j) putchar(',');
        printf("{\"name\":"); string_json(property->pszProperty);
        printf(",\"value_hex\":"); bytes_json(property->pbValue, property->cbValue); putchar('}');
    }
    printf("],\"um\":"); image_ref_json(provider->pUM);
    printf(",\"km\":"); image_ref_json(provider->pKM);
}

int wmain(void) {
    WCHAR path[32768], actual[32768];
    UINT length;
    HMODULE bcrypt, ntdll;
    ResolveProviders resolve;
    QueryRegistration registration;
    FreeBuffer free_buffer;
    QueryProcess query;
    ModeObservation before, after;
    PCRYPT_PROVIDER_REFS refs = NULL, selector_refs = NULL;
    PCRYPT_PROVIDER_REG registrations[MAX_ITEMS] = {0};
    NTSTATUS statuses[MAX_ITEMS] = {0}, resolve_status = 0, selector_status;
    ULONG size = 0, i, count, selector_count;
    LPCWSTR selected_function = NULL;
    BOOL loaded0, loaded1, loaded_selector, loaded2, loaded3;
    if (!SetDefaultDllDirectories(LOAD_LIBRARY_SEARCH_SYSTEM32)) fail("SetDefaultDllDirectories");
    loaded0 = GetModuleHandleW(L"bcryptprimitives.dll") != NULL;
    length = GetSystemDirectoryW(path, (UINT)(sizeof(path) / sizeof(path[0])));
    if (!length || length > 32740) fail("GetSystemDirectoryW");
    memcpy(path + length, L"\\bcrypt.dll", sizeof(L"\\bcrypt.dll"));
    bcrypt = LoadLibraryExW(path, NULL, LOAD_LIBRARY_SEARCH_SYSTEM32);
    if (!bcrypt) fail("restricted bcrypt load");
    length = GetModuleFileNameW(bcrypt, actual, (DWORD)(sizeof(actual) / sizeof(actual[0])));
    if (!length || length >= 32768 || _wcsicmp(path, actual)) fail("bcrypt module path mismatch");
    ntdll = GetModuleHandleW(L"ntdll.dll");
    if (!ntdll) fail("ntdll module missing");
#pragma warning(push)
#pragma warning(disable: 4191) /* GetProcAddress conversion to exact SDK ABI types. */
    resolve = (ResolveProviders)GetProcAddress(bcrypt, "BCryptResolveProviders");
    registration = (QueryRegistration)GetProcAddress(bcrypt, "BCryptQueryProviderRegistration");
    free_buffer = (FreeBuffer)GetProcAddress(bcrypt, "BCryptFreeBuffer");
    query = (QueryProcess)GetProcAddress(ntdll, "NtQueryInformationProcess");
#pragma warning(pop)
    if (!resolve || !registration || !free_buffer || !query) fail("required query export missing");
    loaded1 = GetModuleHandleW(L"bcryptprimitives.dll") != NULL;
    observe_mode(query, &before);
    /* The observed cold selector asks for the first function of interface 6,
     * then opens that function with a NULL implementation. Querying fixed RNG
     * alone would miss a changed default function. This remains a snapshot. */
    selector_status = resolve(NULL, BCRYPT_RNG_INTERFACE, NULL, NULL,
                              CRYPT_UM, 0, &size, &selector_refs);
    loaded_selector = GetModuleHandleW(L"bcryptprimitives.dll") != NULL;
    if (selector_status == 0 && !selector_refs) fail("successful selector returned null");
    selector_count = selector_status == 0 ? selector_refs->cProviders : 0;
    checked_count(selector_count, selector_refs ? selector_refs->rgpProviders : NULL);
    if (selector_count) {
        PCRYPT_PROVIDER_REF first = selector_refs->rgpProviders[0];
        if (!first || !first->pszFunction || !first->pszFunction[0]) fail("selector function missing");
        selected_function = first->pszFunction;
        size = 0;
        resolve_status = resolve(NULL, BCRYPT_RNG_INTERFACE, selected_function, NULL,
                                 CRYPT_UM, CRYPT_ALL_PROVIDERS, &size, &refs);
    }
    loaded2 = GetModuleHandleW(L"bcryptprimitives.dll") != NULL;
    if (selected_function && resolve_status == 0 && !refs) fail("successful resolve returned null");
    count = selected_function && resolve_status == 0 ? refs->cProviders : 0;
    checked_count(count, refs ? refs->rgpProviders : NULL);
    for (i = 0; i < count; ++i) {
        if (!refs->rgpProviders[i] || !refs->rgpProviders[i]->pszProvider) fail("null provider record");
        size = 0;
        statuses[i] = registration(refs->rgpProviders[i]->pszProvider, CRYPT_UM,
                                    BCRYPT_RNG_INTERFACE, &size, &registrations[i]);
    }
    loaded3 = GetModuleHandleW(L"bcryptprimitives.dll") != NULL;
    observe_mode(query, &after);
    printf("{\"schema\":\"localcat.cng-selection-diagnostic.v3\",\"diagnostic_only\":true,"
           "\"limitations\":[\"no_atomic_binding\",\"candidate_bound_mode_query\",\"partial_mode_interpretation\",\"no_preload_authority\"],"
           "\"bcrypt_path\":"); string_json(actual);
    printf(",\"selector\":{\"request\":{\"context\":null,\"interface\":6,\"function\":null,"
           "\"provider\":null,\"mode\":1,\"flags\":0},\"status\":%lu,\"providers\":[", (ULONG)selector_status);
    for (i = 0; i < selector_count; ++i) {
        if (i) putchar(',');
        provider_json(selector_refs->rgpProviders[i]); putchar('}');
    }
    printf("],\"selected_function\":");
    if (selected_function) string_json(selected_function); else printf("null");
    printf("},\"request\":");
    if (selected_function) {
        printf("{\"context\":null,\"interface\":6,\"function\":"); string_json(selected_function);
        printf(",\"provider\":null,\"mode\":1,\"flags\":2}");
    } else printf("null");
    printf(",\"mode_before\":"); mode_json(&before);
    printf(",\"mode_after\":"); mode_json(&after);
    printf(",\"provider_loaded\":{\"before_bcrypt\":%s,\"before_queries\":%s,"
           "\"after_selector\":%s,\"after_resolve\":%s,\"after_registration\":%s},\"resolve_status\":",
           loaded0 ? "true" : "false", loaded1 ? "true" : "false",
           loaded_selector ? "true" : "false", loaded2 ? "true" : "false", loaded3 ? "true" : "false");
    if (selected_function) printf("%lu", (ULONG)resolve_status); else printf("null");
    printf(",\"providers\":[");
    for (i = 0; i < count; ++i) {
        PCRYPT_PROVIDER_REF provider = refs->rgpProviders[i];
        if (i) putchar(',');
        provider_json(provider);
        printf(",\"registration\":"); registration_json(statuses[i], registrations[i]);
        putchar('}');
    }
    printf("]}\n");
    for (i = 0; i < count; ++i) if (registrations[i]) free_buffer(registrations[i]);
    if (refs) free_buffer(refs);
    if (selector_refs) free_buffer(selector_refs);
    FreeLibrary(bcrypt);
    return ferror(stdout) ? 2 : 0; /* Zero means diagnostic emitted, NOT authorization. */
}
