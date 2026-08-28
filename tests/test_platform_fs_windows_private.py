"""Task 3.4 Windows private storage and persistent re-attestation tests."""

from __future__ import annotations

import hashlib
import hmac
import os
import ctypes
from pathlib import Path, PureWindowsPath
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from platform_fs_contracts import (
    DEVICE_SECRET_SIZE_BYTES,
    DeviceSecretAuthority,
    PersistentPrivateProof,
    PlatformFileBackend,
    PlatformFileError,
    PlatformFileErrorCode,
    PrivateProofContext,
    PrivateProofObjectRole,
    PrivateAccessEvidence,
    PrivateStorageProof,
    WINDOWS_PRIVATE_PROOF_SCHEMA,
    WINDOWS_PRIVATE_SECURITY_PROFILE_ID,
    VerifiedPrivateProof,
    WindowsPrivateProof,
    windows_private_proof_mac_message,
)
import platform_fs_windows
from platform_fs_windows import WindowsPlatformAdapter
from tests.windows_process_token_helper import WindowsTestProcessAPI
from windows_file_api import (
    DWORD,
    HANDLE,
    SID_AND_ATTRIBUTES,
    TOKEN_MANDATORY_LABEL,
    Win32Handle,
)


def _assert_private_failure(
    case: unittest.TestCase,
    caught: unittest.case._AssertRaisesContext[PlatformFileError],
) -> None:
    case.assertEqual(
        caught.exception.code,
        PlatformFileErrorCode.PRIVATE_STORAGE_UNPROVEN.value,
    )
    case.assertEqual(
        caught.exception.args,
        (PlatformFileErrorCode.PRIVATE_STORAGE_UNPROVEN.value,),
    )
    case.assertIsNone(caught.exception.__cause__)


class WindowsPrivateStaticTests(unittest.TestCase):
    def test_adapter_exposes_exact_aggregate_and_persistent_shapes(self) -> None:
        adapter = WindowsPlatformAdapter()
        self.assertIsInstance(adapter, PlatformFileBackend)
        self.assertIsInstance(adapter, PrivateStorageProof)
        self.assertIsInstance(adapter, PersistentPrivateProof)

    def test_production_authority_types_are_not_public_state_records(self) -> None:
        for name in (
            "_WindowsPrivateAccessEvidence",
            "_WindowsDeviceSecretAuthority",
            "_WindowsVerifiedPrivateProof",
        ):
            authority_type = getattr(platform_fs_windows, name)
            self.assertNotIn("__dict__", authority_type.__dict__)
            self.assertFalse(
                any(isinstance(member, property) for member in authority_type.__dict__.values())
            )

    def test_public_authority_subclasses_cannot_forge_windows_issuer(self) -> None:
        class ForgedTarget(PrivateAccessEvidence):
            def _close_authority(self) -> None:
                pass

        class ForgedSecret(DeviceSecretAuthority):
            def _reprove(self) -> None:
                pass

            def _close_authority(self) -> None:
                pass

        class ForgedVerified(VerifiedPrivateProof):
            def _close_authority(self) -> None:
                pass

        adapter = WindowsPlatformAdapter()
        context = PrivateProofContext(
            PrivateProofObjectRole.ATTESTATION,
            b"c" * 32,
        )
        target = ForgedTarget()
        secret = ForgedSecret()
        verified = ForgedVerified()
        try:
            with self.assertRaises(PlatformFileError) as caught:
                adapter.mint(target, secret, context)
            _assert_private_failure(self, caught)
            with self.assertRaises(PlatformFileError) as caught:
                adapter.consume_verified(verified, context)
            _assert_private_failure(self, caught)
            self.assertFalse(verified.closed)
        finally:
            target.close()
            secret.close()
            verified.close()

    @unittest.skipUnless(sys.platform == "win32", "token-shape gate requires Windows")
    def test_unsupported_process_primary_token_shapes_fail_closed(self) -> None:
        integrity_pointer = ctypes.c_void_p(1)
        integrity_buffer = ctypes.create_string_buffer(ctypes.sizeof(integrity_pointer))
        ctypes.memmove(
            integrity_buffer,
            ctypes.byref(integrity_pointer),
            ctypes.sizeof(integrity_pointer),
        )
        supported = {
            platform_fs_windows.TOKEN_TYPE_CLASS: platform_fs_windows.TOKEN_PRIMARY,
            platform_fs_windows.TOKEN_SESSION_ID_CLASS: 1,
            platform_fs_windows.TOKEN_ELEVATION_TYPE_CLASS: (
                platform_fs_windows.TOKEN_ELEVATION_TYPE_DEFAULT
            ),
            platform_fs_windows.TOKEN_IS_APP_CONTAINER_CLASS: 0,
        }

        for profile, overrides, restricted, integrity_rid in (
            (
                "impersonation-only",
                {platform_fs_windows.TOKEN_TYPE_CLASS: platform_fs_windows.TOKEN_IMPERSONATION},
                False,
                platform_fs_windows.MEDIUM_INTEGRITY_RID,
            ),
            (
                "service-session-zero",
                {platform_fs_windows.TOKEN_SESSION_ID_CLASS: 0},
                False,
                platform_fs_windows.MEDIUM_INTEGRITY_RID,
            ),
            (
                "app-container",
                {platform_fs_windows.TOKEN_IS_APP_CONTAINER_CLASS: 1},
                False,
                platform_fs_windows.MEDIUM_INTEGRITY_RID,
            ),
            (
                "restricted-primary",
                {},
                True,
                platform_fs_windows.MEDIUM_INTEGRITY_RID,
            ),
            (
                "low-integrity-primary",
                {},
                False,
                0x1000,
            ),
        ):
            with self.subTest(profile=profile):
                values = supported | overrides

                def token_dword(_api: object, _raw: int, information_class: int) -> int:
                    return values[information_class]

                token = Win32Handle(1, _close=lambda _raw: 1, _last_error=lambda: 0)
                api = mock.Mock()
                api.IsTokenRestricted.return_value = restricted
                with mock.patch.object(
                    platform_fs_windows,
                    "_open_current_primary_token",
                    return_value=token,
                ), mock.patch.object(
                    platform_fs_windows,
                    "_token_user_sid",
                    return_value=platform_fs_windows._canonical_sid(5, 21, 1, 2, 3, 1001),
                ), mock.patch.object(
                    platform_fs_windows,
                    "_private_token_dword",
                    side_effect=token_dword,
                ), mock.patch.object(
                    platform_fs_windows,
                    "_private_token_buffer",
                    return_value=integrity_buffer,
                ), mock.patch.object(
                    platform_fs_windows,
                    "_sid_bytes",
                    return_value=platform_fs_windows._canonical_sid(16, integrity_rid),
                ):
                    with self.assertRaises(PlatformFileError) as caught:
                        platform_fs_windows._private_primary_user_sid(api)
                _assert_private_failure(self, caught)


@unittest.skipUnless(sys.platform == "win32", "Task 3.4 requires real Windows")
class WindowsPrivateRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from pathlib import Path

        from tests.windows_native_helper_build import build_native_helper

        cls.native_helper_build = build_native_helper(
            Path(__file__).with_name("windows_lock_access_helper.c"),
            Path(__file__).parents[1]
            / "artifacts"
            / "windows"
            / "task3-4-private-token-matrix",
        )

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root_path = Path(self.temporary.name)
        self.adapter = WindowsPlatformAdapter()
        self.root = self.adapter.bind_root(self.root_path)
        self.parent = self.adapter.bind_parent(
            self.root,
            PureWindowsPath("placeholder.bin"),
        )
        self.test_process_api = WindowsTestProcessAPI.load()

    def tearDown(self) -> None:
        self.parent.close()
        self.root.close()
        self.temporary.cleanup()

    @staticmethod
    def _context(
        role: PrivateProofObjectRole = PrivateProofObjectRole.ATTESTATION,
    ) -> PrivateProofContext:
        return PrivateProofContext(role, hashlib.sha256(b"owner-context").digest())

    def _create_private_regular(self, name: str, payload: bytes) -> None:
        api = self.parent._api
        user_sid = platform_fs_windows._private_primary_user_sid(api)
        path = platform_fs_windows._append_component(
            self.parent._leaf_path,
            name,
            maximum_units=self.parent._maximum_component_units,
        )
        with platform_fs_windows._private_creation_security(api, user_sid) as security:
            handle = api.open_handle(
                path,
                desired_access=(
                    platform_fs_windows.GENERIC_READ
                    | platform_fs_windows.GENERIC_WRITE
                    | platform_fs_windows.READ_CONTROL
                    | platform_fs_windows.SYNCHRONIZE
                ),
                share_mode=0,
                creation_disposition=platform_fs_windows.CREATE_NEW,
                flags=platform_fs_windows.FILE_FLAG_OPEN_REPARSE_POINT,
                security_attributes=security,
            )
        try:
            with handle.borrow() as raw:
                platform_fs_windows._write_exact_payload(api, raw, payload)
                api.checked_bool("FlushFileBuffers", api.FlushFileBuffers, raw)
                platform_fs_windows._private_security_facts(api, raw)
        finally:
            handle.close()

    def _make_low_integrity_token(self, primary: Win32Handle) -> Win32Handle:
        api = self.parent._api
        token_access = (
            0x0001  # TOKEN_ASSIGN_PRIMARY
            | 0x0002  # TOKEN_DUPLICATE
            | 0x0008  # TOKEN_QUERY
            | 0x0080  # TOKEN_ADJUST_DEFAULT
            | 0x0100  # TOKEN_ADJUST_SESSIONID
        )
        value = HANDLE()
        with primary.borrow() as raw:
            api.checked_bool(
                "DuplicateTokenEx",
                api.DuplicateTokenEx,
                raw,
                token_access,
                None,
                2,
                1,
                ctypes.byref(value),
            )
        token = Win32Handle(
            int(value.value or 0),
            _close=api.CloseHandle,
            _last_error=api.last_error,
        )
        low_sid = platform_fs_windows._canonical_sid(16, 4096)
        low_sid_buffer = ctypes.create_string_buffer(low_sid)
        low_label = TOKEN_MANDATORY_LABEL()
        low_label.Label.Sid = ctypes.addressof(low_sid_buffer)
        low_label.Label.Attributes = 0x20
        with token.borrow() as raw:
            api.checked_bool(
                "SetTokenInformation",
                self.test_process_api.SetTokenInformation,
                raw,
                25,
                ctypes.byref(low_label),
                ctypes.sizeof(low_label) + len(low_sid),
            )
        return token

    def _make_restricted_token(self, primary: Win32Handle) -> Win32Handle:
        api = self.parent._api
        with primary.borrow() as raw_primary:
            needed = DWORD()
            self.assertFalse(
                api.GetTokenInformation(
                    raw_primary,
                    2,
                    None,
                    0,
                    ctypes.byref(needed),
                )
            )
            self.assertEqual(api.last_error(), 122)
            groups_buffer = ctypes.create_string_buffer(int(needed.value))
            api.checked_bool(
                "GetTokenInformation",
                api.GetTokenInformation,
                raw_primary,
                2,
                groups_buffer,
                len(groups_buffer),
                ctypes.byref(needed),
            )
        logon_sids = [
            platform_fs_windows._sid_bytes(
                api,
                SID_AND_ATTRIBUTES.from_buffer_copy(
                    groups_buffer.raw,
                    8 + index * ctypes.sizeof(SID_AND_ATTRIBUTES),
                ).Sid,
            )
            for index in range(int.from_bytes(groups_buffer.raw[:4], "little"))
            if int(
                SID_AND_ATTRIBUTES.from_buffer_copy(
                    groups_buffer.raw,
                    8 + index * ctypes.sizeof(SID_AND_ATTRIBUTES),
                ).Attributes
            )
            & 0xC0000000
            == 0xC0000000
        ]
        self.assertEqual(len(logon_sids), 1)
        restrictions = (
            platform_fs_windows._canonical_sid(5, 32, 545),
            platform_fs_windows._canonical_sid(1, 0),
            platform_fs_windows._canonical_sid(5, 11),
            platform_fs_windows._canonical_sid(5, 4),
            logon_sids[0],
        )
        buffers = [ctypes.create_string_buffer(value) for value in restrictions]
        slots = (SID_AND_ATTRIBUTES * len(buffers))(
            *(
                SID_AND_ATTRIBUTES(Sid=ctypes.addressof(buffer), Attributes=0)
                for buffer in buffers
            )
        )
        value = HANDLE()
        with primary.borrow() as raw:
            api.checked_bool(
                "CreateRestrictedToken",
                self.test_process_api.CreateRestrictedToken,
                raw,
                0,
                0,
                None,
                0,
                None,
                len(slots),
                slots,
                ctypes.byref(value),
            )
        return Win32Handle(
            int(value.value or 0),
            _close=api.CloseHandle,
            _last_error=api.last_error,
        )

    def _run_private_child(self, path: Path, token: Win32Handle):
        from tests.test_platform_fs_windows_lock import WindowsPersistentLockTests

        return WindowsPersistentLockTests._run_native_access_helper(self, path, token)

    def test_w2_low_integrity_and_restricted_children_are_denied_private_key_mutation(self) -> None:
        self._create_private_regular("device.key", b"k" * DEVICE_SECRET_SIZE_BYTES)
        path = self.root_path / "device.key"
        original = path.read_bytes()
        api = self.parent._api
        primary = platform_fs_windows._open_current_primary_token(
            api,
            0x0001 | 0x0002 | 0x0008 | 0x0080 | 0x0100,
        )
        low = None
        restricted = None
        try:
            low = self._make_low_integrity_token(primary)
            restricted = self._make_restricted_token(primary)
            for profile, token in (("low", low), ("restricted", restricted)):
                with self.subTest(profile=profile):
                    exit_code, report = self._run_private_child(path, token)
                    self.assertEqual(exit_code, 0)
                    self.assertEqual(report.helper_status, 0)
                    self.assertNotEqual(report.open_result.status, 1)
                    self.assertNotEqual(report.write_result.status, 1)
                    self.assertNotEqual(report.delete_result.status, 1)
                    self.assertTrue(path.exists())
                    self.assertEqual(path.read_bytes(), original)
        finally:
            for token in (restricted, low, primary):
                if token is not None:
                    token.close()

    def test_w2_thread_impersonation_does_not_change_process_primary_subject(self) -> None:
        api = self.parent._api
        expected = platform_fs_windows._private_primary_user_sid(api)
        primary = platform_fs_windows._open_current_primary_token(api, 0x0008 | 0x0002)
        restricted = None
        impersonation = None
        impersonated = False
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        impersonate = advapi32.ImpersonateLoggedOnUser
        impersonate.argtypes = [HANDLE]
        impersonate.restype = ctypes.c_int32
        revert = advapi32.RevertToSelf
        revert.argtypes = []
        revert.restype = ctypes.c_int32
        try:
            restricted = self._make_restricted_token(primary)
            value = HANDLE()
            with restricted.borrow() as raw:
                api.checked_bool(
                    "DuplicateTokenEx",
                    api.DuplicateTokenEx,
                    raw,
                    0x0008 | 0x0004,
                    None,
                    2,
                    2,
                    ctypes.byref(value),
                )
            impersonation = Win32Handle(
                int(value.value or 0),
                _close=api.CloseHandle,
                _last_error=api.last_error,
            )
            with impersonation.borrow() as raw:
                self.assertTrue(impersonate(raw))
                impersonated = True
            self.assertEqual(platform_fs_windows._private_primary_user_sid(api), expected)
        finally:
            if impersonated:
                self.assertTrue(revert())
            if impersonation is not None:
                impersonation.close()
            if restricted is not None:
                restricted.close()
            primary.close()

    def test_w2_elevated_positive_control_is_not_silently_substituted(self) -> None:
        if os.environ.get("LOCALCAT_W2_PROFILE") != "elevated":
            self.skipTest(
                "NOT_RUN: set LOCALCAT_W2_PROFILE=elevated and run from an "
                "elevated PowerShell session"
            )
        api = self.parent._api
        primary = platform_fs_windows._open_current_primary_token(api, 0x0008)
        try:
            with primary.borrow() as raw:
                elevation = platform_fs_windows._private_token_dword(
                    api,
                    raw,
                    platform_fs_windows.TOKEN_ELEVATION_TYPE_CLASS,
                )
                integrity = platform_fs_windows._private_token_buffer(
                    api,
                    raw,
                    platform_fs_windows.TOKEN_INTEGRITY_LEVEL_CLASS,
                )
                label = ctypes.cast(
                    integrity,
                    ctypes.POINTER(ctypes.c_void_p),
                ).contents
                integrity_rid = platform_fs_windows._integrity_rid_from_sid(
                    platform_fs_windows._sid_bytes(api, label.value)
                )
            if (
                elevation != platform_fs_windows.TOKEN_ELEVATION_TYPE_FULL
                or integrity_rid != platform_fs_windows.HIGH_INTEGRITY_RID
            ):
                self.fail(
                    "run this mandatory W2 control from an elevated PowerShell "
                    "session (Start-Process powershell -Verb RunAs); it must not "
                    "be recorded as skipped"
                )
            private = self.adapter.create_private_directory(self.parent, "elevated")
            evidence = self.adapter.prove_private(private)
            evidence.close()
            private.close()
        finally:
            primary.close()

    def _assert_tampered_private_directory_rejected(
        self,
        name: str,
        arguments: tuple[str, ...],
    ) -> None:
        private = self.adapter.create_private_directory(self.parent, name)
        private.close()
        path = self.root_path / name
        result = subprocess.run(
            ["icacls.exe", str(path), *arguments],
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        tampered = self.adapter.bind_parent(
            self.root,
            PureWindowsPath(name, "placeholder.bin"),
        )
        try:
            with self.assertRaises(PlatformFileError) as caught:
                self.adapter.prove_private(tampered)
            _assert_private_failure(self, caught)
        finally:
            tampered.close()

    def test_w2_mic_tamper_fail_closed(self) -> None:
        self._assert_tampered_private_directory_rejected(
            "mic-tamper",
            ("/setintegritylevel", "L"),
        )

    def test_w2_owner_tamper_requires_elevated_profile(self) -> None:
        if os.environ.get("LOCALCAT_W2_PROFILE") != "elevated":
            self.skipTest(
                "NOT_RUN: set LOCALCAT_W2_PROFILE=elevated and run from "
                "an elevated PowerShell session"
            )
        self._assert_tampered_private_directory_rejected(
            "owner-tamper",
            ("/setowner", "*S-1-5-32-544"),
        )

    def test_private_directory_is_exact_profile_and_evidence_outlives_input(self) -> None:
        private = self.adapter.create_private_directory(self.parent, "private")
        evidence = self.adapter.prove_private(private)
        private.close()
        self.assertFalse(evidence.closed)
        evidence.close()
        self.assertTrue(evidence.closed)

    def test_retained_parent_blocks_swap_at_private_directory_creation(self) -> None:
        api = self.parent._api
        original_create = api.CreateDirectoryW
        moved = self.root_path.with_name(self.root_path.name + "-moved")
        swap_errors: list[OSError] = []

        def create_after_swap_attempt(*args: object) -> object:
            try:
                os.rename(self.root_path, moved)
            except OSError as error:
                swap_errors.append(error)
            else:
                os.rename(moved, self.root_path)
                raise AssertionError("retained parent allowed an ancestor swap")
            return original_create(*args)

        with mock.patch.object(
            api,
            "CreateDirectoryW",
            side_effect=create_after_swap_attempt,
        ):
            private = self.adapter.create_private_directory(self.parent, "private")
        try:
            self.assertEqual(len(swap_errors), 1)
            self.assertIsInstance(swap_errors[0], PermissionError)
            self.assertEqual(swap_errors[0].winerror, 32)
            self.assertTrue((self.root_path / "private").is_dir())
            self.assertFalse(moved.exists())
        finally:
            private.close()

    def test_device_secret_outlives_binding_file_and_mint_verify_consume(self) -> None:
        self._create_private_regular("device.key", b"k" * DEVICE_SECRET_SIZE_BYTES)
        secret_file = self.adapter.open_regular(self.root, PureWindowsPath("device.key"))
        secret = self.adapter.bind_device_secret(secret_file)
        secret_file.close()
        secret.reprove()

        private = self.adapter.create_private_directory(self.parent, "attest")
        target = self.adapter.prove_private(private)
        private.close()
        context = self._context()
        proof = self.adapter.mint(target, secret, context)
        self.assertEqual(proof.schema, WINDOWS_PRIVATE_PROOF_SCHEMA)
        self.assertEqual(proof.security_profile_id, WINDOWS_PRIVATE_SECURITY_PROFILE_ID)
        self.assertEqual(proof.object_role, context.object_role)
        self.assertEqual(proof.owner_context_sha256, context.owner_context_sha256)
        self.assertEqual(
            proof.device_secret_mac,
            hmac.digest(
                b"k" * DEVICE_SECRET_SIZE_BYTES,
                windows_private_proof_mac_message(proof),
                "sha256",
            ),
        )

        verified = self.adapter.verify(target, secret, proof, context)
        target.close()
        secret.close()
        self.adapter.consume_verified(verified, context)
        self.assertTrue(verified.closed)
        with self.assertRaises(PlatformFileError) as caught:
            self.adapter.consume_verified(verified, context)
        _assert_private_failure(self, caught)

    def test_restart_rebind_verifies_persisted_proof(self) -> None:
        self._create_private_regular("device.key", b"d" * DEVICE_SECRET_SIZE_BYTES)
        private = self.adapter.create_private_directory(self.parent, "private")
        target = self.adapter.prove_private(private)
        key_file = self.adapter.open_regular(self.root, PureWindowsPath("device.key"))
        secret = self.adapter.bind_device_secret(key_file)
        context = self._context(PrivateProofObjectRole.PRIVATE_DIRECTORY)
        proof = self.adapter.mint(target, secret, context)
        target.close()
        secret.close()
        key_file.close()
        private.close()
        self.parent.close()
        self.root.close()

        restarted = WindowsPlatformAdapter()
        self.root = restarted.bind_root(self.root_path)
        self.parent = restarted.bind_parent(
            self.root,
            PureWindowsPath("placeholder.bin"),
        )
        rebound_private = restarted.bind_parent(
            self.root,
            PureWindowsPath("private/placeholder.bin"),
        )
        rebound_target = restarted.prove_private(rebound_private)
        rebound_key_file = restarted.open_regular(
            self.root,
            PureWindowsPath("device.key"),
        )
        rebound_secret = restarted.bind_device_secret(rebound_key_file)
        verified = restarted.verify(rebound_target, rebound_secret, proof, context)
        restarted.consume_verified(verified, context)
        rebound_target.close()
        rebound_secret.close()
        rebound_key_file.close()
        rebound_private.close()

    def test_foreign_issuer_and_tampered_proof_fail_closed(self) -> None:
        self._create_private_regular("device.key", b"s" * DEVICE_SECRET_SIZE_BYTES)
        private = self.adapter.create_private_directory(self.parent, "private")
        target = self.adapter.prove_private(private)
        key_file = self.adapter.open_regular(self.root, PureWindowsPath("device.key"))
        secret = self.adapter.bind_device_secret(key_file)
        context = self._context()
        proof = self.adapter.mint(target, secret, context)
        with self.assertRaises(PlatformFileError) as caught:
            WindowsPlatformAdapter().mint(target, secret, context)
        _assert_private_failure(self, caught)

        tampered = WindowsPrivateProof(
            schema=proof.schema,
            object_role=proof.object_role,
            security_profile_id=proof.security_profile_id,
            owner_sid_sha256=proof.owner_sid_sha256,
            authority_descriptor_sha256=proof.authority_descriptor_sha256,
            owner_context_sha256=proof.owner_context_sha256,
            device_key_id=proof.device_key_id,
            device_secret_mac=b"x" * 32,
        )
        with self.assertRaises(PlatformFileError) as caught:
            self.adapter.verify(target, secret, tampered, context)
        _assert_private_failure(self, caught)
        target.close()
        secret.close()
        key_file.close()
        private.close()

    def test_non_private_directory_is_rejected(self) -> None:
        (self.root_path / "ordinary").mkdir()
        ordinary = self.adapter.bind_parent(
            self.root,
            PureWindowsPath("ordinary/placeholder.bin"),
        )
        try:
            with self.assertRaises(PlatformFileError) as caught:
                self.adapter.prove_private(ordinary)
            _assert_private_failure(self, caught)
        finally:
            ordinary.close()

    def test_device_secret_size_is_rejected_before_binding_can_mint(self) -> None:
        self._create_private_regular("device.key", b"x" * (DEVICE_SECRET_SIZE_BYTES + 1))
        key_file = self.adapter.open_regular(self.root, PureWindowsPath("device.key"))
        try:
            with self.assertRaises(PlatformFileError) as caught:
                self.adapter.bind_device_secret(key_file)
            _assert_private_failure(self, caught)
        finally:
            key_file.close()

    def test_extra_authorization_ace_is_rejected(self) -> None:
        api = self.parent._api
        user_sid = platform_fs_windows._private_primary_user_sid(api)
        path = platform_fs_windows._append_component(
            self.parent._leaf_path,
            "extra-ace",
            maximum_units=self.parent._maximum_component_units,
        )
        descriptor = platform_fs_windows.PSECURITY_DESCRIPTOR()
        descriptor_size = platform_fs_windows.DWORD()
        sddl = (
            f"O:{platform_fs_windows._sid_string(user_sid)}"
            f"D:P(A;;FA;;;{platform_fs_windows._sid_string(user_sid)})"
            "(A;;FA;;;SY)(A;;FA;;;BA)(A;;FR;;;WD)S:(ML;;NW;;;ME)"
        )
        api.checked_bool(
            "ConvertStringSecurityDescriptorToSecurityDescriptorW",
            api.ConvertStringSecurityDescriptorToSecurityDescriptorW,
            sddl,
            platform_fs_windows.SDDL_REVISION_1,
            platform_fs_windows.ctypes.byref(descriptor),
            platform_fs_windows.ctypes.byref(descriptor_size),
        )
        owned = platform_fs_windows._LocalSecurityDescriptor(api, descriptor)
        try:
            api.checked_bool(
                "CreateDirectoryW",
                api.CreateDirectoryW,
                path,
                platform_fs_windows.ctypes.byref(owned.attributes),
            )
        finally:
            owned.close()
        authority = self.adapter.bind_parent(
            self.root,
            PureWindowsPath("extra-ace/placeholder.bin"),
        )
        try:
            with self.assertRaises(PlatformFileError) as caught:
                self.adapter.prove_private(authority)
            _assert_private_failure(self, caught)
        finally:
            authority.close()


if __name__ == "__main__":
    unittest.main()
