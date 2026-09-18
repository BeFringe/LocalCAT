#ifndef LOCALCAT_NATIVE_CLOSURE_H
#define LOCALCAT_NATIVE_CLOSURE_H

#include <stddef.h>
#include <stdint.h>
#include <wchar.h>

/* File proof ONLY: no loader, provider selection, or E4 authorization. */
struct localcat_system_file_candidate {
    const wchar_t *basename;
    uint64_t size;
    unsigned char sha256[32];
};
struct localcat_system_files;

/* *output must be NULL. Candidates come from the compiled, bound caller.
 * No environment, JSON, registry, or inferred dependency list is consumed. */
int localcat_system_files_prepare(const struct localcat_system_file_candidate *candidates,
    size_t count, struct localcat_system_files **output, const char **diagnostic);
int localcat_system_files_reprove(struct localcat_system_files *files, const char **diagnostic);
/* Idempotent; clears the caller's pointer. All APIs are owner-thread only. */
void localcat_system_files_close(struct localcat_system_files **files);

#endif
