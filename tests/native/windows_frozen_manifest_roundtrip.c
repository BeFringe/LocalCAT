#include "localcat_manifest.h"
#include <stdio.h>
#include <string.h>

int main(void)
{
    unsigned char bytes[8192];
    FILE *file = NULL;
    size_t count;
    struct localcat_runtime_manifest manifest;
    const struct localcat_manifest_entry *entry;
    const char *diagnostic = NULL;
    if (fopen_s(&file, "runtime.manifest", "rb") != 0 || file == NULL) return 1;
    count = fread(bytes, 1, sizeof(bytes), file);
    if (ferror(file) || !feof(file)) { fclose(file); return 2; }
    fclose(file);
    if (localcat_manifest_parse(&manifest, bytes, count, &diagnostic) != 0) {
        fprintf(stderr, "%s\n", diagnostic);
        return 3;
    }
    if (manifest.entry_count != 2 || memcmp(manifest.root_digest,
            localcat_embedded_runtime_root_digest, 32) != 0) return 4;
    entry = localcat_manifest_find(&manifest, "bootstrap");
    if (entry == NULL || entry->role != LOCALCAT_ROLE_BOOTSTRAP ||
            entry->byte_count != 4 || entry->dependency_count != 1 ||
            entry->path_length != strlen("_internal\\bootstrap.py") ||
            memcmp(entry->path, "_internal\\bootstrap.py", entry->path_length) != 0 ||
            entry->dependencies[0] != 1) return 5;
    entry = localcat_manifest_find(&manifest, "fixture");
    if (entry == NULL || entry->role != LOCALCAT_ROLE_FIXTURE || entry->byte_count != 3) return 6;
    return 0;
}
