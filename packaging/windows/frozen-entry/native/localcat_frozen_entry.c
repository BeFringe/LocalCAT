#ifdef _WIN32

#include "localcat_frozen_entry.h"
#include "localcat_frozen_bootstrap.h"
#include "localcat_sha256.h"

#include <stdlib.h>
#include <string.h>
#include <wchar.h>

static unsigned char entry_started;

static void localcat_trace_event(struct localcat_sha256_context *trace, const char *event)
{
    /* NUL separates fixed event names; no paths, addresses or business text. */
    localcat_sha256_update(trace, event, strlen(event) + 1U);
}

static void localcat_trace_u64(struct localcat_sha256_context *trace, uint64_t value)
{
    unsigned char encoded[8];
    unsigned int index;
    for (index = 0; index < 8U; ++index) {
        encoded[index] = (unsigned char)(value >> (index * 8U));
    }
    localcat_sha256_update(trace, encoded, sizeof(encoded));
}

static int localcat_digest_from_hex(const char *text, unsigned char digest[32])
{
    unsigned int index;
    if (strlen(text) != 64U) { return -1; }
    for (index = 0; index < 32U; ++index) {
        unsigned int half, value = 0U;
        for (half = 0; half < 2U; ++half) {
            unsigned char ch = (unsigned char)text[index * 2U + half];
            unsigned int digit;
            if (ch >= '0' && ch <= '9') { digit = ch - '0'; }
            else if (ch >= 'a' && ch <= 'f') { digit = ch - 'a' + 10U; }
            else { return -1; }
            value = value * 16U + digit;
        }
        digest[index] = (unsigned char)value;
    }
    return 0;
}

static int localcat_trace_retained(struct localcat_sha256_context *trace,
    const struct localcat_bundle_authority *authority)
{
    unsigned int index, root_index;
    for (root_index = 0; root_index < authority->directory_count; ++root_index) {
        if (wcscmp(authority->root_final_path, authority->directory_final_paths[root_index]) == 0) { break; }
    }
    if (root_index == authority->directory_count) { return -1; }
    localcat_trace_event(trace, "E2.ROOT_IDENTITY");
    localcat_trace_u64(trace, authority->directory_identities[root_index].volume_serial);
    localcat_sha256_update(trace,
        authority->directory_identities[root_index].file_id, 16U);
    localcat_trace_u64(trace, authority->executable_identity.volume_serial);
    localcat_sha256_update(trace, authority->executable_identity.file_id, 16U);
    localcat_trace_event(trace, "E3.MANIFEST_RETAINED");
    localcat_sha256_update(trace, authority->manifest.root_digest, 32U);
    for (index = 0; index < authority->entry_count; ++index) {
        const struct localcat_retained_entry *entry = &authority->entries[index];
        localcat_trace_event(trace, "E4_E5.ENTRY_RETAINED");
        localcat_trace_u64(trace, entry->manifest_entry->id_length);
        localcat_sha256_update(trace, entry->manifest_entry->id, entry->manifest_entry->id_length);
        localcat_trace_u64(trace, entry->manifest_entry->role);
        localcat_trace_u64(trace, entry->manifest_entry->byte_count);
        localcat_trace_u64(trace, entry->identity.volume_serial);
        localcat_sha256_update(trace, entry->identity.file_id, 16U);
        localcat_sha256_update(trace, entry->manifest_entry->digest, 32U);
    }
    return 0;
}

int localcat_frozen_entry(void)
{
    struct localcat_bundle_authority *authority;
    struct localcat_retained_entry *python;
    struct localcat_bootstrap_facts facts;
    struct localcat_sha256_context trace, build;
    const char *diagnostic;
    unsigned int index;

    /* E1: no LocalCAT allocation, path/environment read or loader call precedes
     * this policy. Static CRT startup is a separate E0.5 build audit. */
    if (!SetDefaultDllDirectories(LOAD_LIBRARY_SEARCH_SYSTEM32)) {
        localcat_diagnostic_marker("FROZEN_ENTRY.DLL_POLICY_FAILED");
        return 1;
    }
    if (entry_started) {
        localcat_diagnostic_marker("FROZEN_ENTRY.ENTRY_REUSED");
        return 1;
    }
    entry_started = 1U;
    diagnostic = "FROZEN_ENTRY.ENTRY_FAILED";
    authority = NULL;
    memset(&facts, 0, sizeof(facts));
    facts.abi_version = LOCALCAT_BOOTSTRAP_ABI_VERSION;
    facts.struct_size = sizeof(facts);
    if (localcat_digest_from_hex(LOCALCAT_CANDIDATE_INPUT_DIGEST_HEX, facts.candidate_digest) != 0 ||
        localcat_digest_from_hex(LOCALCAT_PRELINK_INPUT_DIGEST_HEX, facts.prelink_digest) != 0) {
        diagnostic = "FROZEN_ENTRY.EMBEDDED_INPUT_INVALID";
        goto failure;
    }
    /* Build identity binds the pre-link source/toolchain/runtime inputs, not
     * the resulting PE (whose digest belongs to the post-build manifest). */
    localcat_sha256_init(&build);
    localcat_trace_event(&build, "localcat.frozen-bootloader-build.v1");
    localcat_sha256_update(&build, facts.candidate_digest, 32U);
    localcat_sha256_update(&build, facts.prelink_digest, 32U);
    localcat_sha256_final(&build, facts.build_id);
    localcat_sha256_init(&trace);
    localcat_trace_event(&trace, "localcat.frozen-entry-trace.v1");
    localcat_trace_event(&trace, "E1.SYSTEM32_POLICY_SET");
    if (localcat_initialization_environment_check(&diagnostic) != 0) { goto failure; }
    authority = calloc(1U, sizeof(*authority));
    if (authority == NULL) { diagnostic = "FROZEN_ENTRY.ALLOCATION_FAILED"; goto failure; }
    if (localcat_bundle_authority_prepare(authority, &diagnostic) != 0) { goto failure; }
    if (localcat_trace_retained(&trace, authority) != 0) {
        diagnostic = "FROZEN_ENTRY.ROOT_TRACE_UNBOUND";
        goto failure;
    }
    python = localcat_bundle_authority_find(authority, "python-runtime");
    if (python == NULL || python->manifest_entry->role != LOCALCAT_ROLE_NATIVE) {
        diagnostic = "FROZEN_ENTRY.PYTHON_ENTRY_MISSING";
        goto failure;
    }
    for (index = 0; index < authority->entry_count; ++index) {
        struct localcat_retained_entry *entry = &authority->entries[index];
        if (entry->manifest_entry->role != LOCALCAT_ROLE_NATIVE) { continue; }
        if (localcat_native_closure_load_verified(entry, &diagnostic) == NULL) { goto failure; }
        localcat_trace_event(&trace, "E6_E7.NATIVE_LOADED_REPROVED");
        localcat_trace_u64(&trace, entry->manifest_entry->id_length);
        localcat_sha256_update(&trace, entry->manifest_entry->id, entry->manifest_entry->id_length);
        localcat_sha256_update(&trace, entry->identity.file_id, 16U);
    }
    if (localcat_bootstrap_bind(python->loaded_module, &diagnostic) != 0) { goto failure; }
    localcat_trace_event(&trace, "E8.CAPI_BOUND");
    if (localcat_bootstrap_configure(authority, &facts, &diagnostic) != 0) { goto failure; }
    authority = NULL; /* Cleanup ownership transferred to the native producer. */
    if (localcat_bootstrap_initialize(&diagnostic) != 0) { goto failure; }
    localcat_trace_event(&trace, "E9.ISOLATED_RETAINED_INITIALIZATION");
    if (localcat_bootstrap_arm(&diagnostic) != 0) { goto failure; }
    localcat_trace_event(&trace, "E10.PRODUCER_ARMED");
    localcat_sha256_final(&trace, facts.trace_digest);
    if (localcat_bootstrap_seal_trace(facts.trace_digest, &diagnostic) != 0) { goto failure; }
    if (localcat_bootstrap_execute(&diagnostic) != 0) { goto failure; }
    localcat_bootstrap_abort();
    /* Optional observer output, never a user runtime admission condition. */
    (void)localcat_stage_marker("FROZEN_ENTRY.SPIKE_COMPLETED");
    return 0;

failure:
    if (authority != NULL) {
        localcat_bundle_authority_close(authority);
        free(authority);
    }
    localcat_bootstrap_abort();
    localcat_diagnostic_marker(diagnostic);
    return 1;
}

#endif
