#ifndef LOCALCAT_MANIFEST_H
#define LOCALCAT_MANIFEST_H

#include <stddef.h>
#include <stdint.h>

#define LOCALCAT_MANIFEST_MAX_ENTRIES 32U
#define LOCALCAT_MANIFEST_HEADER_SIZE 80U
#define LOCALCAT_MANIFEST_ENTRY_SIZE 72U

/* Replaced in the pristine build copy by the manifest generator. */
#define LOCALCAT_CANDIDATE_INPUT_DIGEST_HEX "@@CANDIDATE_INPUT_DIGEST@@"
#define LOCALCAT_PRELINK_INPUT_DIGEST_HEX "@@PRELINK_INPUT_DIGEST@@"
static const unsigned char localcat_embedded_runtime_manifest_digest[32] = {@@RUNTIME_MANIFEST_DIGEST@@};
static const unsigned char localcat_embedded_runtime_root_digest[32] = {@@RUNTIME_ROOT_DIGEST@@};

enum localcat_manifest_role {
    LOCALCAT_ROLE_NATIVE = 1,
    LOCALCAT_ROLE_INTERPRETER = 2,
    LOCALCAT_ROLE_BOOTSTRAP = 3,
    LOCALCAT_ROLE_CRITICAL_SOURCE = 4,
    LOCALCAT_ROLE_FIXTURE = 5
};

struct localcat_manifest_entry {
    const unsigned char *id;
    uint32_t id_length;
    const unsigned char *path;
    uint32_t path_length;
    uint32_t role;
    uint64_t byte_count;
    unsigned char digest[32];
    const unsigned char *dependencies;
    uint32_t dependency_count;
};

struct localcat_runtime_manifest {
    const unsigned char *bytes;
    size_t byte_count;
    uint32_t entry_count;
    struct localcat_manifest_entry entries[LOCALCAT_MANIFEST_MAX_ENTRIES];
    unsigned char root_digest[32];
};

/* Structural parser, not a runtime authority issuer. The rooted caller must
 * authenticate the complete bytes against the embedded runtime manifest digest
 * before consuming these views. Returned slices borrow the input buffer. */
int localcat_manifest_parse(
    struct localcat_runtime_manifest *manifest,
    const unsigned char *bytes,
    size_t byte_count,
    const char **diagnostic
);
const struct localcat_manifest_entry *localcat_manifest_find(
    const struct localcat_runtime_manifest *manifest,
    const char *entry_id
);

#endif
