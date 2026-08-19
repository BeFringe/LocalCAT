#include <CoreFoundation/CoreFoundation.h>
#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

static int read_bundle_path(CFStringRef key, char output[PATH_MAX]) {
    CFBundleRef bundle = CFBundleGetMainBundle();
    if (bundle == NULL) {
        return 0;
    }
    CFTypeRef value = CFBundleGetValueForInfoDictionaryKey(bundle, key);
    if (value == NULL || CFGetTypeID(value) != CFStringGetTypeID()) {
        return 0;
    }
    return CFStringGetFileSystemRepresentation(
        (CFStringRef)value,
        output,
        PATH_MAX
    );
}

int main(int argc, char **argv) {
    char python[PATH_MAX];
    char bootstrap[PATH_MAX];
    if (!read_bundle_path(CFSTR("LocalCATPythonExecutable"), python) ||
        !read_bundle_path(CFSTR("LocalCATBootstrapPath"), bootstrap)) {
        fputs("LocalCAT startup failed: bundle bootstrap binding is invalid.\n", stderr);
        return 72;
    }
    if (access(python, X_OK) != 0 || access(bootstrap, R_OK) != 0) {
        fputs("LocalCAT startup failed: configured runtime is unavailable.\n", stderr);
        return 72;
    }
    char **arguments = calloc((size_t)argc + 2, sizeof(char *));
    if (arguments == NULL) {
        fputs("LocalCAT startup failed: argument allocation failed.\n", stderr);
        return 72;
    }
    arguments[0] = python;
    arguments[1] = bootstrap;
    for (int index = 1; index < argc; ++index) {
        arguments[index + 1] = argv[index];
    }
    arguments[argc + 1] = NULL;
    execv(python, arguments);
    fprintf(stderr, "LocalCAT startup failed: runtime error %d.\n", errno);
    return 72;
}
