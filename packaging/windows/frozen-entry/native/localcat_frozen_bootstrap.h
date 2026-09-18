#ifndef LOCALCAT_FROZEN_BOOTSTRAP_H
#define LOCALCAT_FROZEN_BOOTSTRAP_H

/* This translation unit uses the exact pinned CPython types, not PyInstaller's
 * opaque compatibility typedefs. Never implicitly import python314.dll. */
#ifndef Py_NO_LINK_LIB
#define Py_NO_LINK_LIB 1
#endif
#include <Python.h>
#include "localcat_rooted_io.h"

#define LOCALCAT_BOOTSTRAP_ABI_VERSION 1U
struct localcat_bootstrap_facts {
    uint32_t abi_version;
    uint32_t struct_size;
    unsigned char build_id[32];
    unsigned char candidate_digest[32];
    unsigned char prelink_digest[32];
    unsigned char trace_digest[32];
};

int localcat_bootstrap_bind(HMODULE python, const char **diagnostic);
/* Successful configure transfers the heap authority's cleanup ownership.
 * This is a native-only producer API; it does not yet permit Python take. */
int localcat_bootstrap_configure(struct localcat_bundle_authority *authority,
    const struct localcat_bootstrap_facts *facts, const char **diagnostic);
int localcat_bootstrap_register(PyInitConfig *config, const char **diagnostic);
/* Native producer registration of the retained interpreter-start source set. */
int localcat_bootstrap_register_sources(PyInitConfig *config, const char **diagnostic);
/* E9 composition after caller's E4/E7 proofs: owns the isolated configuration.
 * Does not itself claim an E0/E4 system closure or constitute the PE entry. */
int localcat_bootstrap_initialize(const char **diagnostic);
/* Called by the native entry only after E9. Revalidates all E7 native results. */
int localcat_bootstrap_arm(const char **diagnostic);
/* Native entry seals its actual E1-E9/arm trace once, before E10 take. */
int localcat_bootstrap_seal_trace(const unsigned char digest[32], const char **diagnostic);
/* E11: execute the retained bootstrap once, only after native arm succeeds. */
int localcat_bootstrap_execute(const char **diagnostic);
/* Stable native marker only; never Python exception text or source contents. */
const char *localcat_bootstrap_last_diagnostic(void);
/* Idempotent native failure cleanup; never re-arms the producer. */
void localcat_bootstrap_abort(void);

#endif
