#include <CoreFoundation/CoreFoundation.h>
#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define LOCALCAT_DIRECT_PYTHON "LOCALCAT_DIRECT_PYTHON"
#define LOCALCAT_DIRECT_BOOTSTRAP "LOCALCAT_DIRECT_BOOTSTRAP"
#define LOCALCAT_NATIVE_LAUNCH "LOCALCAT_NATIVE_LAUNCH"
#define LOCALCAT_DIRECT_PYTHON_ARGUMENT "--localcat-direct-python"
#define LOCALCAT_DIRECT_BOOTSTRAP_ARGUMENT "--localcat-direct-bootstrap"

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

static int copy_override_path(const char *value, char output[PATH_MAX]) {
    if (value == NULL || value[0] != '/') {
        return 0;
    }
    size_t length = strlen(value);
    if (length == 0 || length >= PATH_MAX) {
        return 0;
    }
    memcpy(output, value, length + 1);
    return 1;
}

int main(int argc, char **argv) {
    char python[PATH_MAX];
    char bootstrap[PATH_MAX];
    int forwarded_argument_start = 1;
    const char *python_override = getenv(LOCALCAT_DIRECT_PYTHON);
    const char *bootstrap_override = getenv(LOCALCAT_DIRECT_BOOTSTRAP);
    if (argc >= 5 &&
        strcmp(argv[1], LOCALCAT_DIRECT_PYTHON_ARGUMENT) == 0 &&
        strcmp(argv[3], LOCALCAT_DIRECT_BOOTSTRAP_ARGUMENT) == 0) {
        python_override = argv[2];
        bootstrap_override = argv[4];
        forwarded_argument_start = 5;
    } else if ((argc >= 2 &&
                strcmp(argv[1], LOCALCAT_DIRECT_PYTHON_ARGUMENT) == 0) ||
               (argc >= 4 &&
                strcmp(argv[3], LOCALCAT_DIRECT_BOOTSTRAP_ARGUMENT) == 0)) {
        fputs("LocalCAT startup failed: direct handoff arguments are invalid.\n", stderr);
        return 72;
    }
    int has_python_override = python_override != NULL;
    int has_bootstrap_override = bootstrap_override != NULL;
    if (has_python_override != has_bootstrap_override) {
        fputs("LocalCAT startup failed: direct handoff binding is incomplete.\n", stderr);
        return 72;
    }
    if (has_python_override) {
        if (!copy_override_path(python_override, python) ||
            !copy_override_path(bootstrap_override, bootstrap)) {
            fputs("LocalCAT startup failed: direct handoff binding is invalid.\n", stderr);
            return 72;
        }
    } else if (!read_bundle_path(CFSTR("LocalCATPythonExecutable"), python) ||
               !read_bundle_path(CFSTR("LocalCATBootstrapPath"), bootstrap)) {
        fputs("LocalCAT startup failed: bundle bootstrap binding is invalid.\n", stderr);
        return 72;
    }
    if (access(python, X_OK) != 0 || access(bootstrap, R_OK) != 0) {
        fputs("LocalCAT startup failed: configured runtime is unavailable.\n", stderr);
        return 72;
    }
    size_t forwarded_count = (size_t)(argc - forwarded_argument_start);
    char **arguments = calloc(forwarded_count + 3, sizeof(char *));
    if (arguments == NULL) {
        fputs("LocalCAT startup failed: argument allocation failed.\n", stderr);
        return 72;
    }
    arguments[0] = python;
    arguments[1] = bootstrap;
    for (size_t index = 0; index < forwarded_count; ++index) {
        arguments[index + 2] = argv[forwarded_argument_start + (int)index];
    }
    arguments[forwarded_count + 2] = NULL;
    unsetenv(LOCALCAT_DIRECT_PYTHON);
    unsetenv(LOCALCAT_DIRECT_BOOTSTRAP);
    if (setenv(LOCALCAT_NATIVE_LAUNCH, "1", 1) != 0) {
        fputs("LocalCAT startup failed: native launch identity is unavailable.\n", stderr);
        return 72;
    }
    execv(python, arguments);
    fprintf(stderr, "LocalCAT startup failed: runtime error %d.\n", errno);
    return 72;
}
