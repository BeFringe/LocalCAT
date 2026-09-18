#include "localcat_manifest.h"

#include <limits.h>
#include <string.h>
#include <windows.h>

static uint32_t
localcat_read_u32(const unsigned char *value)
{
    return (uint32_t)value[0] | ((uint32_t)value[1] << 8U) | ((uint32_t)value[2] << 16U) | ((uint32_t)value[3] << 24U);
}

static uint64_t
localcat_read_u64(const unsigned char *value)
{
    uint64_t result = 0;
    unsigned int index;
    for (index = 0; index < 8; ++index) {
        result |= (uint64_t)value[index] << (index * 8U);
    }
    return result;
}

static int
localcat_checked_range(size_t total, uint32_t offset, uint32_t count, uint32_t item_size, size_t *end)
{
    size_t length;
    if (item_size != 0U && (size_t)count > SIZE_MAX / item_size) {
        return -1;
    }
    length = (size_t)count * item_size;
    if ((size_t)offset > total || length > total - (size_t)offset) {
        return -1;
    }
    *end = (size_t)offset + length;
    return 0;
}

static int
localcat_valid_utf8(const unsigned char *value, uint32_t size)
{
    uint32_t index = 0;
    while (index < size) {
        unsigned char first = value[index++];
        uint32_t codepoint;
        uint32_t remaining;
        if (first < 0x80U) {
            if (first == 0U) {
                return 0;
            }
            continue;
        }
        if (first >= 0xc2U && first <= 0xdfU) {
            codepoint = first & 0x1fU; remaining = 1;
        } else if (first >= 0xe0U && first <= 0xefU) {
            codepoint = first & 0x0fU; remaining = 2;
        } else if (first >= 0xf0U && first <= 0xf4U) {
            codepoint = first & 0x07U; remaining = 3;
        } else {
            return 0;
        }
        if (remaining > size - index) {
            return 0;
        }
        while (remaining-- > 0U) {
            unsigned char next = value[index++];
            if ((next & 0xc0U) != 0x80U) {
                return 0;
            }
            codepoint = (codepoint << 6U) | (next & 0x3fU);
        }
        if (codepoint > 0x10ffffU || (codepoint >= 0xd800U && codepoint <= 0xdfffU)) {
            return 0;
        }
        if ((codepoint < 0x80U) || (codepoint < 0x800U && first >= 0xe0U) || (codepoint < 0x10000U && first >= 0xf0U)) {
            return 0;
        }
    }
    return 1;
}

static int
localcat_valid_id(const unsigned char *value, uint32_t size)
{
    uint32_t index;
    if (size == 0U || size > 63U) {
        return 0;
    }
    for (index = 0; index < size; ++index) {
        unsigned char character = value[index];
        if (!((character >= 'a' && character <= 'z') || (character >= '0' && character <= '9') || character == '-')) {
            return 0;
        }
    }
    return 1;
}

static int
localcat_reserved_component(const unsigned char *value, uint32_t size)
{
    unsigned char base[8];
    uint32_t length = 0, index;
    while (length < size && value[length] != '.') { ++length; }
    while (length > 0U && value[length - 1U] == ' ') { --length; }
    if (length >= sizeof(base)) { return 0; }
    for (index = 0; index < length; ++index) {
        unsigned char c = value[index];
        base[index] = (c >= 'a' && c <= 'z') ? (unsigned char)(c - 'a' + 'A') : c;
    }
    if (length == 3U && (memcmp(base, "CON", 3) == 0 || memcmp(base, "PRN", 3) == 0 ||
        memcmp(base, "AUX", 3) == 0 || memcmp(base, "NUL", 3) == 0)) { return 1; }
    if (length >= 4U && (memcmp(base, "COM", 3) == 0 || memcmp(base, "LPT", 3) == 0)) {
        if (length == 4U && base[3] >= '1' && base[3] <= '9') { return 1; }
        if (length == 5U && base[3] == 0xc2U &&
            (base[4] == 0xb9U || base[4] == 0xb2U || base[4] == 0xb3U)) { return 1; }
    }
    return 0;
}

static int
localcat_valid_relative_path(const unsigned char *value, uint32_t size)
{
    uint32_t index;
    uint32_t component_start = 0;
    if (size == 0U || size > 1024U || !localcat_valid_utf8(value, size)) {
        return 0;
    }
    for (index = 0; index <= size; ++index) {
        unsigned char character = index < size ? value[index] : '\\';
        if (index < size && (character == '/' || character == ':' || character < 0x20U ||
            character == '<' || character == '>' || character == '"' || character == '|' ||
            character == '?' || character == '*')) {
            return 0;
        }
        if (character == '\\') {
            uint32_t component_size = index - component_start;
            if (component_size == 0U || (component_size == 1U && value[component_start] == '.') ||
                (component_size == 2U && value[component_start] == '.' && value[component_start + 1] == '.')) {
                return 0;
            }
            if (value[index - 1] == '.' || value[index - 1] == ' ') {
                return 0;
            }
            if (localcat_reserved_component(value + component_start, component_size)) { return 0; }
            {
                wchar_t component[1025];
                int length = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS,
                    (const char *)value + component_start, (int)component_size, component, 1025);
                if (length <= 0 || length > 255) { return 0; }
            }
            component_start = index + 1U;
        }
    }
    return 1;
}

static int
localcat_same_string(const unsigned char *left, uint32_t left_size, const unsigned char *right, uint32_t right_size)
{
    return left_size == right_size && memcmp(left, right, left_size) == 0;
}

static int
localcat_same_windows_name(const unsigned char *left, uint32_t left_size, const unsigned char *right, uint32_t right_size)
{
    wchar_t left_wide[1025], right_wide[1025];
    int left_length = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS,
        (const char *)left, (int)left_size, left_wide, 1025);
    int right_length = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS,
        (const char *)right, (int)right_size, right_wide, 1025);
    int comparison;
    if (left_length <= 0 || right_length <= 0) { return 1; }
    comparison = CompareStringOrdinal(left_wide, left_length, right_wide, right_length, TRUE);
    /* An unavailable comparison must never turn a possible alias into authority. */
    return comparison == 0 || comparison == CSTR_EQUAL;
}

static int
localcat_same_basename(const struct localcat_manifest_entry *left, const struct localcat_manifest_entry *right)
{
    uint32_t left_start = left->path_length;
    uint32_t right_start = right->path_length;
    while (left_start > 0U && left->path[left_start - 1U] != '\\') { --left_start; }
    while (right_start > 0U && right->path[right_start - 1U] != '\\') { --right_start; }
    return localcat_same_windows_name(
        left->path + left_start, left->path_length - left_start,
        right->path + right_start, right->path_length - right_start
    );
}

static int
localcat_ranges_overlap(uint32_t left, uint32_t left_size, uint32_t right, uint32_t right_size)
{
    /* Called only after checked_range; use 64-bit ends even for a 32-bit build. */
    return left_size != 0U && right_size != 0U &&
        (uint64_t)left < (uint64_t)right + right_size &&
        (uint64_t)right < (uint64_t)left + left_size;
}

static int
localcat_dependency_cycle_visit(
    const struct localcat_runtime_manifest *manifest,
    uint32_t entry_index,
    unsigned char *state
)
{
    const struct localcat_manifest_entry *entry = &manifest->entries[entry_index];
    uint32_t index;
    if (state[entry_index] == 1U) { return -1; }
    if (state[entry_index] == 2U) { return 0; }
    state[entry_index] = 1U;
    for (index = 0; index < entry->dependency_count; ++index) {
        uint32_t dependency = localcat_read_u32(entry->dependencies + (index * 4U));
        if (dependency >= manifest->entry_count || localcat_dependency_cycle_visit(manifest, dependency, state) != 0) {
            return -1;
        }
    }
    state[entry_index] = 2U;
    return 0;
}

int
localcat_manifest_parse(
    struct localcat_runtime_manifest *manifest,
    const unsigned char *bytes,
    size_t byte_count,
    const char **diagnostic
)
{
    uint32_t entry_count, entry_size, entries_offset, dependencies_offset, dependency_count;
    uint32_t strings_offset, strings_size, flags;
    size_t entries_end, dependencies_end, strings_end;
    uint32_t index, other;
    uint64_t referenced_strings = 0, referenced_dependencies = 0;
    unsigned char cycle_state[LOCALCAT_MANIFEST_MAX_ENTRIES] = {0};

    *diagnostic = "FROZEN_ENTRY.MANIFEST_INVALID";
    memset(manifest, 0, sizeof(*manifest));
    if (byte_count < LOCALCAT_MANIFEST_HEADER_SIZE || memcmp(bytes, "LCFMV001", 8) != 0) {
        return -1;
    }
    if (localcat_read_u32(bytes + 8) != 1U || localcat_read_u32(bytes + 12) != LOCALCAT_MANIFEST_HEADER_SIZE) {
        return -1;
    }
    entry_count = localcat_read_u32(bytes + 16);
    entry_size = localcat_read_u32(bytes + 20);
    entries_offset = localcat_read_u32(bytes + 24);
    dependencies_offset = localcat_read_u32(bytes + 28);
    dependency_count = localcat_read_u32(bytes + 32);
    strings_offset = localcat_read_u32(bytes + 36);
    strings_size = localcat_read_u32(bytes + 40);
    flags = localcat_read_u32(bytes + 44);
    if (entry_count == 0U || entry_count > LOCALCAT_MANIFEST_MAX_ENTRIES || entry_size != LOCALCAT_MANIFEST_ENTRY_SIZE || flags != 0U) {
        return -1;
    }
    if (memcmp(bytes + 48, localcat_embedded_runtime_root_digest, 32) != 0) {
        *diagnostic = "FROZEN_ENTRY.MANIFEST_ROOT_MISMATCH";
        return -1;
    }
    if (localcat_checked_range(byte_count, entries_offset, entry_count, entry_size, &entries_end) != 0 ||
        localcat_checked_range(byte_count, dependencies_offset, dependency_count, 4U, &dependencies_end) != 0 ||
        localcat_checked_range(byte_count, strings_offset, strings_size, 1U, &strings_end) != 0) {
        return -1;
    }
    if (entries_offset != LOCALCAT_MANIFEST_HEADER_SIZE || dependencies_offset != entries_end || strings_offset != dependencies_end || strings_end != byte_count) {
        *diagnostic = "FROZEN_ENTRY.MANIFEST_OVERLAP_OR_TRAILING";
        return -1;
    }

    manifest->bytes = bytes;
    manifest->byte_count = byte_count;
    manifest->entry_count = entry_count;
    memcpy(manifest->root_digest, bytes + 48, 32);
    for (index = 0; index < entry_count; ++index) {
        const unsigned char *record = bytes + entries_offset + ((size_t)index * entry_size);
        struct localcat_manifest_entry *entry = &manifest->entries[index];
        uint32_t path_offset = localcat_read_u32(record);
        uint32_t id_offset = localcat_read_u32(record + 8);
        uint32_t dependency_offset = localcat_read_u32(record + 24);
        size_t ignored_end;
        entry->path_length = localcat_read_u32(record + 4);
        entry->id_length = localcat_read_u32(record + 12);
        entry->role = localcat_read_u32(record + 16);
        entry->dependency_count = localcat_read_u32(record + 28);
        entry->byte_count = localcat_read_u64(record + 32);
        memcpy(entry->digest, record + 40, 32);
        if (localcat_read_u32(record + 20) != 0U || entry->role < LOCALCAT_ROLE_NATIVE || entry->role > LOCALCAT_ROLE_FIXTURE) {
            return -1;
        }
        if (localcat_checked_range(strings_size, path_offset, entry->path_length, 1U, &ignored_end) != 0 ||
            localcat_checked_range(strings_size, id_offset, entry->id_length, 1U, &ignored_end) != 0 ||
            localcat_checked_range(dependency_count, dependency_offset, entry->dependency_count, 1U, &ignored_end) != 0) {
            return -1;
        }
        if (localcat_ranges_overlap(path_offset, entry->path_length, id_offset, entry->id_length)) {
            *diagnostic = "FROZEN_ENTRY.MANIFEST_OVERLAP_OR_TRAILING";
            return -1;
        }
        for (other = 0; other < index; ++other) {
            const unsigned char *previous = bytes + entries_offset + ((size_t)other * entry_size);
            uint32_t previous_path = localcat_read_u32(previous);
            uint32_t previous_id = localcat_read_u32(previous + 8);
            uint32_t previous_dependency = localcat_read_u32(previous + 24);
            const struct localcat_manifest_entry *prior = &manifest->entries[other];
            if (localcat_ranges_overlap(path_offset, entry->path_length, previous_path, prior->path_length) ||
                localcat_ranges_overlap(path_offset, entry->path_length, previous_id, prior->id_length) ||
                localcat_ranges_overlap(id_offset, entry->id_length, previous_path, prior->path_length) ||
                localcat_ranges_overlap(id_offset, entry->id_length, previous_id, prior->id_length) ||
                localcat_ranges_overlap(dependency_offset, entry->dependency_count,
                    previous_dependency, prior->dependency_count)) {
                *diagnostic = "FROZEN_ENTRY.MANIFEST_OVERLAP_OR_TRAILING";
                return -1;
            }
        }
        entry->path = bytes + strings_offset + path_offset;
        entry->id = bytes + strings_offset + id_offset;
        entry->dependencies = bytes + dependencies_offset + ((size_t)dependency_offset * 4U);
        if (!localcat_valid_relative_path(entry->path, entry->path_length) || !localcat_valid_id(entry->id, entry->id_length)) {
            *diagnostic = "FROZEN_ENTRY.MANIFEST_PATH_OR_UTF8_INVALID";
            return -1;
        }
        for (other = 0; other < index; ++other) {
            if (localcat_same_string(entry->id, entry->id_length, manifest->entries[other].id, manifest->entries[other].id_length) ||
                localcat_same_windows_name(entry->path, entry->path_length, manifest->entries[other].path, manifest->entries[other].path_length) ||
                localcat_same_basename(entry, &manifest->entries[other])) {
                *diagnostic = "FROZEN_ENTRY.MANIFEST_DUPLICATE";
                return -1;
            }
        }
        referenced_strings += (uint64_t)entry->path_length + entry->id_length;
        referenced_dependencies += entry->dependency_count;
    }
    /* Every slice is in-bounds and pairwise disjoint above. Equality of their
     * total lengths therefore proves exact coverage, including internal gaps. */
    if (referenced_strings != strings_size || referenced_dependencies != dependency_count) {
        *diagnostic = "FROZEN_ENTRY.MANIFEST_UNREFERENCED_POOL";
        return -1;
    }
    for (index = 0; index < entry_count; ++index) {
        if (localcat_dependency_cycle_visit(manifest, index, cycle_state) != 0) {
            *diagnostic = "FROZEN_ENTRY.MANIFEST_DEPENDENCY_CYCLE";
            return -1;
        }
    }
    return 0;
}

const struct localcat_manifest_entry *
localcat_manifest_find(const struct localcat_runtime_manifest *manifest, const char *entry_id)
{
    uint32_t index;
    size_t id_length = strlen(entry_id);
    if (id_length > UINT32_MAX) {
        return NULL;
    }
    for (index = 0; index < manifest->entry_count; ++index) {
        const struct localcat_manifest_entry *entry = &manifest->entries[index];
        if (localcat_same_string(entry->id, entry->id_length, (const unsigned char *)entry_id, (uint32_t)id_length)) {
            return entry;
        }
    }
    return NULL;
}
