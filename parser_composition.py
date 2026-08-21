"""Explicit Parser registry composition seam.

Wave 1 intentionally registers no built-in codecs.  Public callers provide configured
providers; trusted built-ins enter only through this module's private composition seam.
There is no discovery fallback.
"""

from __future__ import annotations

from dataclasses import dataclass

from parser_contracts import CodecDescriptor, CodecProvider, ContractViolation
from parser_registry import ParserRegistry


class ProviderConfigurationError(ContractViolation):
    """Body-safe failure for an explicitly configured provider binding."""


@dataclass(frozen=True, slots=True)
class ProviderBinding:
    provider_id: str
    provider: CodecProvider | None
    enabled: bool
    compatible_versions: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.provider_id) is not str or not self.provider_id:
            raise ProviderConfigurationError(
                "PARSER.SELECTION.PROVIDER_INVALID",
                "configured provider identity must be a non-empty string",
            )
        if self.provider_id != self.provider_id.strip():
            raise ProviderConfigurationError(
                "PARSER.SELECTION.PROVIDER_INVALID",
                "configured provider identity must not contain surrounding whitespace",
            )
        if type(self.enabled) is not bool:
            raise ProviderConfigurationError(
                "PARSER.SELECTION.PROVIDER_INVALID",
                "configured provider enabled state must be an exact boolean",
            )
        if type(self.compatible_versions) is not tuple:
            raise ProviderConfigurationError(
                "PARSER.SELECTION.PROVIDER_INVALID",
                "provider version allowlist must be an immutable tuple",
            )
        if not self.compatible_versions:
            raise ProviderConfigurationError(
                "PARSER.SELECTION.PROVIDER_INVALID",
                "provider version allowlist must not be empty",
            )
        for version in self.compatible_versions:
            if type(version) is not str or not version or version != version.strip():
                raise ProviderConfigurationError(
                    "PARSER.SELECTION.PROVIDER_INVALID",
                    "provider version allowlist contains an invalid version",
                )
        if len(set(self.compatible_versions)) != len(self.compatible_versions):
            raise ProviderConfigurationError(
                "PARSER.SELECTION.PROVIDER_INVALID",
                "provider version allowlist must not contain duplicates",
            )
        object.__setattr__(
            self,
            "compatible_versions",
            tuple(sorted(self.compatible_versions)),
        )


def compose_registry(
    *,
    providers: tuple[ProviderBinding, ...] = (),
    descriptors: object = None,
) -> ParserRegistry:
    """Build the Wave 1 empty/bound-provider registry without discovery fallback.

    ``descriptors`` is a fail-closed compatibility trap: external descriptors must
    use ``ProviderBinding``.  Task 4.1 will feed built-ins through the private trusted
    seam in this module after their codec implementations exist.
    """

    if descriptors is not None:
        raise ProviderConfigurationError(
            "PARSER.SELECTION.DIRECT_DESCRIPTOR_FORBIDDEN",
            "external codec descriptors must be supplied by a configured provider",
        )
    return _compose_from_trusted_builtins((), providers=providers)


def _compose_from_trusted_builtins(
    builtin_descriptors: tuple[CodecDescriptor, ...],
    *,
    providers: tuple[ProviderBinding, ...],
) -> ParserRegistry:
    """Internal-only seam reserved for Task 4.1's explicit built-in imports."""

    if type(builtin_descriptors) is not tuple:
        raise ProviderConfigurationError(
            "PARSER.SELECTION.DESCRIPTOR_INVALID",
            "trusted built-in descriptors must be supplied as an immutable tuple",
        )
    if type(providers) is not tuple:
        raise ProviderConfigurationError(
            "PARSER.SELECTION.PROVIDER_INVALID",
            "provider bindings must be supplied as an immutable tuple",
        )
    for binding in providers:
        if type(binding) is not ProviderBinding:
            raise ProviderConfigurationError(
                "PARSER.SELECTION.PROVIDER_INVALID",
                "provider bindings must use the explicit neutral composition contract",
            )

    provider_ids = tuple(sorted(binding.provider_id for binding in providers))
    if len(set(provider_ids)) != len(provider_ids):
        raise ProviderConfigurationError(
            "PARSER.SELECTION.PROVIDER_DUPLICATE",
            "the same provider identity was configured more than once",
        )

    collected = list(builtin_descriptors)
    for binding in sorted(providers, key=lambda item: item.provider_id):
        collected.extend(_load_provider(binding))
    return ParserRegistry(tuple(collected))


def _load_provider(binding: ProviderBinding) -> tuple[CodecDescriptor, ...]:
    if not binding.enabled:
        raise ProviderConfigurationError(
            "PARSER.SELECTION.PROVIDER_DISABLED",
            "the configured codec provider is disabled",
        )
    provider = binding.provider
    if provider is None:
        raise ProviderConfigurationError(
            "PARSER.SELECTION.PROVIDER_MISSING",
            "the configured codec provider is not available",
        )

    try:
        provider_id = provider.provider_id
        provider_version = provider.provider_version
        descriptors_method = provider.descriptors
    except Exception:
        raise ProviderConfigurationError(
            "PARSER.SELECTION.PROVIDER_INVALID",
            "the configured codec provider does not satisfy the neutral provider contract",
        ) from None

    if not isinstance(provider, CodecProvider):
        raise ProviderConfigurationError(
            "PARSER.SELECTION.PROVIDER_INVALID",
            "the configured codec provider does not satisfy the neutral provider contract",
        )

    if (
        type(provider_id) is not str
        or provider_id != binding.provider_id
        or not provider_id
    ):
        raise ProviderConfigurationError(
            "PARSER.SELECTION.PROVIDER_IDENTITY_MISMATCH",
            "configured and published provider identities do not match",
        )
    if (
        type(provider_version) is not str
        or not provider_version
        or provider_version != provider_version.strip()
    ):
        raise ProviderConfigurationError(
            "PARSER.SELECTION.PROVIDER_INVALID",
            "the codec provider publishes an invalid version",
        )
    if provider_version not in binding.compatible_versions:
        raise ProviderConfigurationError(
            "PARSER.SELECTION.PROVIDER_VERSION_INCOMPATIBLE",
            "the codec provider version is outside the configured compatibility allowlist",
        )
    if not callable(descriptors_method):
        raise ProviderConfigurationError(
            "PARSER.SELECTION.PROVIDER_INVALID",
            "the codec provider does not publish a descriptor factory",
        )

    try:
        descriptors = descriptors_method()
    except Exception:
        raise ProviderConfigurationError(
            "PARSER.SELECTION.PROVIDER_FAILED",
            "the codec provider failed while publishing descriptors",
        ) from None
    if type(descriptors) is not tuple:
        raise ProviderConfigurationError(
            "PARSER.SELECTION.PROVIDER_INVALID",
            "the codec provider must publish an immutable descriptor tuple",
        )
    for descriptor in descriptors:
        if type(descriptor) is not CodecDescriptor:
            raise ProviderConfigurationError(
                "PARSER.SELECTION.PROVIDER_INVALID",
                "the codec provider published a non-contract descriptor",
            )
        if descriptor.identity.provider_id != provider_id:
            raise ProviderConfigurationError(
                "PARSER.SELECTION.PROVIDER_IDENTITY_MISMATCH",
                "provider and descriptor identities do not match",
            )
    return descriptors
