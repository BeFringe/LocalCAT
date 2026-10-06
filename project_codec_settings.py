"""Device-local known-provider settings and explicit Application composition.

The settings contain no execution locations or project data. Compatibility is
product policy here, never authority imported from a project or settings file.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile

from parser_composition import (
    ParserApplicationSurface, ProviderBinding, ProviderConfigurationError,
    create_parser_application_surface,
)
from parser_contracts import (
    CodecCapabilities, CodecDescriptor, CodecIdentity, CodecProvider,
    EffectivePurpose, FormatId, InputConsumptionPolicy,
)


_PROVIDER_ID = 'localcat.rpy'
_COMPATIBLE_PROVIDER_VERSIONS = ('1',)
_CODEC_IDENTITY = CodecIdentity(_PROVIDER_ID, 'renpy-tl', '1')
_FORMAT_ID = FormatId('renpy-tl-v1')
_MAX_SETTINGS_BYTES = 16 * 1024


class CodecSettingsError(ValueError):
    """Body-safe failure of the device settings document."""


@dataclass(frozen=True, slots=True)
class CodecProviderSetting:
    provider_id: str
    enabled: bool

    def __post_init__(self) -> None:
        if type(self.provider_id) is not str or self.provider_id != _PROVIDER_ID:
            raise ValueError('unknown project codec provider')
        if type(self.enabled) is not bool:
            raise TypeError('provider enabled state must be an exact boolean')


@dataclass(frozen=True, slots=True)
class CodecSettings:
    providers: tuple[CodecProviderSetting, ...] = (CodecProviderSetting(_PROVIDER_ID, True),)

    def __post_init__(self) -> None:
        if (type(self.providers) is not tuple or len(self.providers) != 1
                or type(self.providers[0]) is not CodecProviderSetting):
            raise ValueError('settings must contain exactly the known provider')


class CodecSettingsRepository:
    def __init__(self, config_dir: Path) -> None:
        self.config_dir = config_dir.expanduser().resolve()
        self.settings_path = self.config_dir / 'project-codecs.json'

    def load(self) -> CodecSettings:
        try:
            with self.settings_path.open('rb') as handle:
                raw = handle.read(_MAX_SETTINGS_BYTES + 1)
        except FileNotFoundError:
            return CodecSettings()
        except OSError:
            raise CodecSettingsError('unable to read project codec settings') from None
        try:
            if len(raw) > _MAX_SETTINGS_BYTES:
                raise ValueError('settings limit exceeded')
            value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object)
            if type(value) is not dict or set(value) != {'providers'}:
                raise ValueError('invalid settings schema')
            providers = value['providers']
            if type(providers) is not list or len(providers) != 1:
                raise ValueError('invalid provider list')
            provider = providers[0]
            if type(provider) is not dict or set(provider) != {'provider_id', 'enabled'}:
                raise ValueError('invalid provider schema')
            return CodecSettings((CodecProviderSetting(provider['provider_id'], provider['enabled']),))
        except (ValueError, TypeError, RecursionError):
            raise CodecSettingsError('invalid project codec settings') from None

    def save(self, settings: CodecSettings) -> None:
        if type(settings) is not CodecSettings:
            raise TypeError('settings must be exact CodecSettings')
        payload = {'providers': [
            {'provider_id': provider.provider_id, 'enabled': provider.enabled}
            for provider in settings.providers
        ]}
        temporary = None
        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode='w', encoding='utf-8', dir=self.config_dir,
                prefix='.project-codecs.', suffix='.tmp', delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.settings_path)
        except OSError:
            raise CodecSettingsError('unable to save project codec settings') from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate settings key')
        value[key] = item
    return value


@dataclass(frozen=True, slots=True)
class ProjectCodecAvailability:
    codec_identity: CodecIdentity
    format_id: FormatId
    available: bool
    code: str | None
    safe_summary: str


@dataclass(frozen=True, slots=True)
class ProjectCodecRuntime:
    """One configuration snapshot; recompose to compare current device facts.

    settings and availability are immutable values suitable for invalidating a
    later Application preview. Neither they nor the live surface enter a package.
    """

    settings: CodecSettings | None
    availability: tuple[ProjectCodecAvailability, ...]
    surface: ParserApplicationSurface


def _bundled_rpy_provider() -> CodecProvider | None:
    # This is the sole product edge to the optional codec implementation.
    try:
        from parser_rpy_codec import RpyProvider
    except ImportError:
        # A missing dependency of this explicit optional provider is also a
        # missing installation. Built-in imports happen outside this boundary.
        return None
    return RpyProvider()


def _compatible_provider(provider: CodecProvider) -> bool:
    try:
        descriptors = provider.descriptors()
        return (
            provider.provider_id == _PROVIDER_ID
            and provider.provider_version in _COMPATIBLE_PROVIDER_VERSIONS
            and type(descriptors) is tuple and len(descriptors) == 1
            and type(descriptors[0]) is CodecDescriptor
            and descriptors[0].identity == _CODEC_IDENTITY
            and descriptors[0].format_id == _FORMAT_ID
            and descriptors[0].extensions == ('.rpy',)
            and descriptors[0].limit_profile.profile_version == 1
            and descriptors[0].purpose is EffectivePurpose.PROJECT_DOCUMENT
            and descriptors[0].input_consumption_policy is InputConsumptionPolicy.SEALED_BYTES_EOF
            and descriptors[0].capabilities == CodecCapabilities(
                readable=True, validatable=True, canonical_write=False,
                source_round_trip_write=True, streaming_input=False,
                iterator_view=True, materialized_view=True, format_profile='renpy-tl-v1',
            )
            and descriptors[0].source_state_factory is not None
            and descriptors[0].round_trip_serializer_factory is not None
        )
    except Exception:
        return False


def compose_project_codec_runtime(config_dir: Path) -> ProjectCodecRuntime:
    """Keep built-ins usable when the optional product provider is unavailable."""
    try:
        settings = CodecSettingsRepository(config_dir).load()
    except CodecSettingsError:
        settings = None
    code = None
    summary = 'Ren\'Py TL is available.'
    bindings = ()
    if settings is None:
        code, summary = 'PROVIDER_CONFIGURATION_INVALID', 'Project codec settings are invalid or unreadable.'
    elif not settings.providers[0].enabled:
        code, summary = 'PROVIDER_DISABLED', 'Ren\'Py TL support is disabled on this device.'
    else:
        provider = _bundled_rpy_provider()
        if provider is None:
            code, summary = 'PROVIDER_MISSING', 'Ren\'Py TL support is missing from this installation.'
        elif not _compatible_provider(provider):
            code, summary = 'PROVIDER_INCOMPATIBLE', 'Ren\'Py TL support has an incompatible version or profile.'
        else:
            bindings = (ProviderBinding(_PROVIDER_ID, provider, True, _COMPATIBLE_PROVIDER_VERSIONS),)
    try:
        surface = create_parser_application_surface(providers=bindings)
    except ProviderConfigurationError:
        code, summary = 'PROVIDER_INCOMPATIBLE', 'Ren\'Py TL support does not satisfy the provider contract.'
        surface = create_parser_application_surface()
    availability = ProjectCodecAvailability(
        _CODEC_IDENTITY, _FORMAT_ID, code is None,
        'PARSER.SELECTION.' + code if code is not None else None, summary,
    )
    return ProjectCodecRuntime(settings, (availability,), surface)
