#ifndef LOCALCAT_ROOTED_IO_H
#define LOCALCAT_ROOTED_IO_H

#ifdef _WIN32

#include <windows.h>
#include <stddef.h>
#include <stdint.h>

#include "localcat_manifest.h"

#define LOCALCAT_MAX_RETAINED_DIRECTORIES 64U

struct localcat_file_identity {
    uint64_t volume_serial;
    unsigned char file_id[16];
};

struct localcat_retained_entry {
    struct localcat_bundle_authority *authority;
    const struct localcat_manifest_entry *manifest_entry;
    HANDLE handle;
    struct localcat_file_identity identity;
    wchar_t final_path[32768];
    HMODULE loaded_module;
    unsigned char actual_module_reproved;
    unsigned char digest_proved;
};

struct localcat_bundle_authority {
    HANDLE directory_handles[LOCALCAT_MAX_RETAINED_DIRECTORIES];
    struct localcat_file_identity directory_identities[LOCALCAT_MAX_RETAINED_DIRECTORIES];
    wchar_t *directory_final_paths[LOCALCAT_MAX_RETAINED_DIRECTORIES];
    unsigned int directory_count;
    wchar_t root_final_path[32768];
    wchar_t executable_final_path[32768];
    HANDLE executable_handle;
    struct localcat_file_identity executable_identity;
    wchar_t manifest_final_path[32768];
    HANDLE manifest_handle;
    struct localcat_file_identity manifest_identity;
    struct localcat_runtime_manifest manifest;
    unsigned char *manifest_bytes;
    struct localcat_retained_entry entries[LOCALCAT_MANIFEST_MAX_ENTRIES];
    unsigned int entry_count;
    unsigned long owner_thread;
    void *owner_interpreter;
    unsigned char state;
    unsigned char all_entries_proved;
};

int localcat_bundle_authority_prepare(struct localcat_bundle_authority *authority, const char **diagnostic);
/* Observation gate only: does not pin absence of future child additions. */
int localcat_bundle_authority_recheck_inventory(struct localcat_bundle_authority *authority, const char **diagnostic);
void localcat_bundle_authority_close(struct localcat_bundle_authority *authority);
struct localcat_retained_entry *localcat_bundle_authority_find(struct localcat_bundle_authority *authority, const char *id);
int localcat_retained_entry_read_verified(
    struct localcat_retained_entry *entry,
    unsigned char **bytes,
    size_t *byte_count,
    const char **diagnostic
);
int localcat_retained_entry_reprove(
    struct localcat_retained_entry *entry,
    const char **diagnostic
);
int localcat_initialization_environment_check(const char **diagnostic);
HMODULE localcat_native_closure_load_verified(
    struct localcat_retained_entry *entry,
    const char **diagnostic
);
void localcat_diagnostic_marker(const char *marker);
/* Native progress state is independent of observer availability. Only an
 * invalid marker is rejected; stderr/debug output is best effort, never modal. */
int localcat_stage_marker(const char *marker);
/* Entry-thread diagnostic state, not an authority or a persistent log. */
const char *localcat_last_stage_marker(void);

#endif
#endif
