#ifndef LOCALCAT_SHA256_H
#define LOCALCAT_SHA256_H

#include <stddef.h>
#include <stdint.h>

struct localcat_sha256_context {
    uint32_t state[8];
    uint64_t bit_count;
    unsigned char block[64];
    size_t block_size;
};

void localcat_sha256_init(struct localcat_sha256_context *context);
void localcat_sha256_update(struct localcat_sha256_context *context, const void *data, size_t size);
void localcat_sha256_final(struct localcat_sha256_context *context, unsigned char digest[32]);
int localcat_sha256_equal(const unsigned char left[32], const unsigned char right[32]);

#endif
