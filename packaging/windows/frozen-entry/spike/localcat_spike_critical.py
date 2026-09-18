"""Task 1.6 source-only critical module.

The native authority, rather than a pathname or bytecode cache, supplies the
bytes that compile this module.
"""

EXPECTED_FIXTURE = b"LocalCAT frozen authority fixture v1\n"


def verify_fixture(payload):
    return payload == EXPECTED_FIXTURE
