#include "localcat_sha256.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int main(int argc, char **argv)
{
    struct localcat_sha256_context context;
    unsigned char *data, digest[32], changed[32];
    size_t length, chunk, offset, index;
    if (argc != 3) { return 2; }
    length = (size_t)strtoul(argv[1], NULL, 10);
    chunk = (size_t)strtoul(argv[2], NULL, 10);
    if (length > 1000000U || chunk == 0U) { return 2; }
    data = (unsigned char *)malloc(length + 1U);
    if (data == NULL) { return 2; }
    for (index = 0; index < length; ++index) {
        data[index] = (unsigned char)((index * 73U + 19U) & 255U);
    }
    localcat_sha256_init(&context);
    localcat_sha256_update(&context, NULL, 0);
    for (offset = 0; offset < length;) {
        size_t take = chunk < length - offset ? chunk : length - offset;
        localcat_sha256_update(&context, data + offset, take);
        offset += take;
    }
    localcat_sha256_final(&context, digest);
    free(data);
    for (index = 0; index < sizeof(context); ++index) {
        if (((const unsigned char *)&context)[index] != 0U) { return 3; }
    }
    if (!localcat_sha256_equal(digest, digest)) { return 4; }
    for (index = 0; index < sizeof(digest); ++index) {
        memcpy(changed, digest, sizeof(digest));
        changed[index] ^= 1U;
        if (localcat_sha256_equal(digest, changed)) { return 5; }
    }
    for (index = 0; index < sizeof(digest); ++index) {
        printf("%02x", digest[index]);
    }
    puts("");
    return 0;
}
