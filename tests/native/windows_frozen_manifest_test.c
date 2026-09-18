#include "localcat_manifest.h"

#include <stdio.h>
#include <string.h>

static unsigned char input[8192];
static unsigned int input_size;
static unsigned int checks;
static unsigned int failures;

static void put32(unsigned int at, unsigned int value)
{
    unsigned int i;
    for (i = 0; i < 4; ++i) { input[at + i] = (unsigned char)(value >> (8 * i)); }
}

static unsigned int get32(unsigned int at)
{
    return (unsigned int)input[at] | ((unsigned int)input[at + 1] << 8) |
        ((unsigned int)input[at + 2] << 16) | ((unsigned int)input[at + 3] << 24);
}

static void fixture(const char *first, const char *second)
{
    const char *paths[2] = {first, second};
    const char *ids[2] = {"first", "second"};
    unsigned int i, used = 0;
    const unsigned int strings = 224;
    memset(input, 0, sizeof(input));
    memcpy(input, "LCFMV001", 8);
    put32(8, 1); put32(12, 80); put32(16, 2); put32(20, 72);
    put32(24, 80); put32(28, 224); put32(32, 0); put32(36, strings);
    memcpy(input + 48, localcat_embedded_runtime_root_digest, 32);
    for (i = 0; i < 2; ++i) {
        unsigned int record = 80 + i * 72;
        unsigned int length = (unsigned int)strlen(paths[i]);
        put32(record, used); put32(record + 4, length);
        memcpy(input + strings + used, paths[i], length); used += length;
        length = (unsigned int)strlen(ids[i]);
        put32(record + 8, used); put32(record + 12, length);
        memcpy(input + strings + used, ids[i], length); used += length;
        put32(record + 16, LOCALCAT_ROLE_NATIVE);
    }
    put32(40, used); input_size = strings + used;
}

static void set_dependency_pool(unsigned int count)
{
    unsigned int old_count = get32(32), old_strings = get32(36), dependencies = get32(28);
    unsigned int new_strings = dependencies + count * 4;
    memmove(input + new_strings, input + old_strings, get32(40));
    if (count > old_count) { memset(input + dependencies + old_count * 4, 0, (count - old_count) * 4); }
    put32(32, count); put32(36, new_strings); input_size = new_strings + get32(40);
}

static void check(const char *name, int expected_success)
{
    struct localcat_runtime_manifest manifest;
    const char *diagnostic = NULL;
    int success = localcat_manifest_parse(&manifest, input, input_size, &diagnostic) == 0;
    ++checks;
    if (success != expected_success) {
        ++failures;
        printf("FAIL %s: expected %s, received %s\n", name,
            expected_success ? "accept" : "reject", success ? "accept" : diagnostic);
    }
}

static void append_terminal_entry(void)
{
    unsigned int used = get32(40);
    const char path[] = "z\\last.dll";
    const char id[] = "last";
    memmove(input + 304, input + 232, used);
    memmove(input + 296, input + 224, 8);
    memset(input + 224, 0, 72);
    put32(16, 3); put32(28, 296); put32(36, 304);
    put32(224, used); put32(228, sizeof(path) - 1);
    memcpy(input + 304 + used, path, sizeof(path) - 1); used += sizeof(path) - 1;
    put32(232, used); put32(236, sizeof(id) - 1);
    memcpy(input + 304 + used, id, sizeof(id) - 1); used += sizeof(id) - 1;
    put32(240, LOCALCAT_ROLE_NATIVE);
    put32(40, used); input_size = 304 + used;
}

int main(void)
{
    const char *invalid[] = {
        "x\\CON", "x\\con.txt", "x\\PRN", "x\\AUX.py", "x\\NUL.txt",
        "x\\COM1.dll", "x\\lpt9.dll", "x\\COM\xc2\xb9.dll", "x\\LPT\xc2\xb2.dll",
        "x\\COM\xc2\xb3.dll", "x\\CON .txt", "x\\a?.dll", "x\\a*.dll",
        "x\\a<.dll", "x\\a>.dll", "x\\a|.dll", "x\\a\".dll",
        "x\\a:.dll", "x\\a/.dll", "x\\a.\\b", "x\\a ",
        "x\\..\\b", "x\\.\\b", "\\absolute", "x\\\\b", "x\\bad\xc0\xaf"
    };
    unsigned int i;
    char long_component[257];
    fixture("x\\python314.dll", "y\\helper.dll"); check("valid", 1);
    fixture("x\\COM10.dll", "y\\LPT0.dll"); check("non-reserved numbered names", 1);
    fixture("x\\python314.dll", "y\\PYTHON314.DLL"); check("case-insensitive basename", 0);
    fixture("x\\python314.dll", "X\\PYTHON314.DLL"); check("case-insensitive path", 0);
    fixture("x\\\xc3\xa4.dll", "y\\\xc3\x84.dll"); check("Unicode case-insensitive basename", 0);
    for (i = 0; i < sizeof(invalid) / sizeof(invalid[0]); ++i) {
        fixture(invalid[i], "y\\helper.dll"); check(invalid[i], 0);
    }
    fixture("x\\first", "y\\helper.dll");
    put32(88, 2); check("path/id string overlap", 0);
    fixture("x\\first", "y\\helper.dll");
    put32(152, 0); put32(156, 1); check("cross-entry string overlap", 0);
    fixture("x\\first", "y\\helper.dll");
    set_dependency_pool(2);
    append_terminal_entry();
    put32(104, 0); put32(108, 1); put32(176, 1); put32(180, 1);
    put32(296, 2); put32(300, 2); check("valid shared dependency target with separate slices", 1);
    put32(104, 0); put32(108, 1); put32(176, 0); put32(180, 1);
    check("acyclic dependency slice overlap", 0);
    put32(176, 2); set_dependency_pool(3); put32(304, 2);
    check("internal dependency pool gap", 0);
    fixture("x\\first", "y\\helper.dll");
    set_dependency_pool(1);
    put32(104, 0); put32(108, 1); put32(224, 1); check("valid dependency", 1);
    set_dependency_pool(2);
    put32(176, 1); put32(180, 1); put32(228, 0); check("dependency cycle", 0);
    fixture("x\\first", "y\\helper.dll");
    put32(104, 0xffffffffU); put32(108, 2); check("dependency range overflow", 0);
    fixture("x\\first", "y\\helper.dll");
    put32(80, 0xffffffffU); check("string range overflow", 0);
    fixture("x\\first", "y\\helper.dll");
    put32(28, 223); check("global region overlap", 0);
    fixture("x\\first", "y\\helper.dll");
    put32(44, 1); check("header unknown flag", 0);
    fixture("x\\first", "y\\helper.dll");
    put32(100, 1); check("entry unknown flag", 0);
    fixture("x\\first", "y\\helper.dll");
    put32(96, 99); check("unknown role", 0);
    fixture("x\\first", "y\\helper.dll");
    input[input_size++] = 0; check("trailing byte", 0);
    fixture("x\\first", "y\\helper.dll");
    --input_size; check("truncated strings", 0);
    fixture("x\\first", "y\\helper.dll");
    put32(160, get32(88)); put32(164, get32(92)); check("duplicate id", 0);
    memset(long_component, 'a', 255); long_component[255] = 0;
    fixture(long_component, "y\\helper.dll"); check("255 UTF-16 unit component", 1);
    long_component[255] = 'a'; long_component[256] = 0;
    fixture(long_component, "y\\helper.dll"); check("overlong Windows component", 0);
    fixture("x\\first", "y\\helper.dll");
    input[48] ^= 1; check("embedded root mismatch", 0);
    fixture("x\\first", "y\\helper.dll");
    put32(16, 0xffffffffU); check("entry count overflow", 0);
    fixture("x\\first", "y\\helper.dll");
    set_dependency_pool(2); check("unused dependency pool words", 0);
    fixture("x\\first", "y\\helper.dll");
    put32(84, get32(84) - 1); check("internal string pool gap", 0);
    fixture("x\\first", "y\\helper.dll");
    input[input_size++] = 'x'; put32(40, get32(40) + 1); check("hidden trailing string pool byte", 0);
    fixture("x\\first", "y\\helper.dll");
    set_dependency_pool(2); put32(108, 1); put32(224, 1);
    check("hidden trailing dependency pool word", 0);
    printf("manifest parser: %u checks, %u failures\n", checks, failures);
    return failures ? 1 : 0;
}
