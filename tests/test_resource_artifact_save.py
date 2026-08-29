from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from platform_fs import compose_platform_file_backend
from platform_fs_contracts import PlatformFileError, PlatformFileErrorCode
from resource_artifact_save import ResourceArtifactSaveService
from resource_package_contracts import ResourcePortabilityError
from resource_platform_io import platform_relative_path


class ResourceArtifactSaveTests(unittest.TestCase):
    def test_symlink_and_hardlink_destinations_are_rejected_without_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            source = root / "existing.csv"
            source.write_bytes(b"old")
            aliases = [("hardlink.csv", lambda path: path.hardlink_to(source))]
            if os.name != "nt":
                aliases.append(("symlink.csv", lambda path: path.symlink_to(source)))
            for name, make_destination in aliases:
                with self.subTest(name=name):
                    candidate = root / f".{name}.candidate"
                    candidate.write_bytes(b"new")
                    destination = root / name
                    make_destination(destination)
                    with self.assertRaises(ResourcePortabilityError) as caught:
                        ResourceArtifactSaveService().publish(
                            candidate,
                            destination,
                            lambda path: path.read_bytes(),
                            owner_commit=lambda _publication, _validation: None,
                        )
                    self.assertEqual(
                        caught.exception.code,
                        "RESOURCE.EXPORT.DESTINATION_STALE",
                    )
                    self.assertEqual(source.read_bytes(), b"old")
                    self.assertEqual(candidate.read_bytes(), b"new")
                    destination.unlink()
            if os.name == "nt":
                target = root / "junction-target"
                target.mkdir()
                junction = root / "junction"
                subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
                    check=True,
                    capture_output=True,
                )
                candidate = junction / ".candidate"
                candidate.write_bytes(b"new")
                destination = junction / "resource.csv"
                with self.assertRaises(ResourcePortabilityError) as caught:
                    ResourceArtifactSaveService().publish(
                        candidate,
                        destination,
                        lambda path: path.read_bytes(),
                        owner_commit=lambda _publication, _validation: None,
                    )
                self.assertEqual(
                    caught.exception.code,
                    "RESOURCE.EXPORT.PUBLICATION_FAILED",
                )

    def test_parent_inode_replacement_before_publication_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw).resolve()
            root = base / "export"
            moved = base / "moved"
            root.mkdir()
            candidate = root / ".candidate"
            destination = root / "resource.csv"
            candidate.write_bytes(b"new")
            destination.write_bytes(b"old")
            swapped = False

            def validator(path: Path) -> bytes:
                nonlocal swapped
                payload = path.read_bytes()
                if not swapped:
                    swapped = True
                    root.rename(moved)
                    root.mkdir()
                return payload

            with self.assertRaises(ResourcePortabilityError) as caught:
                ResourceArtifactSaveService().publish(
                    candidate,
                    destination,
                    validator,
                    owner_commit=lambda _publication, _validation: None,
                )
            self.assertEqual(
                caught.exception.code,
                (
                    "RESOURCE.EXPORT.VALIDATION_FAILED"
                    if os.name == "nt"
                    else "RESOURCE.EXPORT.DESTINATION_STALE"
                ),
            )
            retained_parent = root if os.name == "nt" else moved
            self.assertEqual((retained_parent / "resource.csv").read_bytes(), b"old")
            self.assertEqual((retained_parent / ".candidate").read_bytes(), b"new")

    def test_post_publish_validation_failure_restores_exact_prior_destination(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            candidate = root / ".candidate"
            destination = root / "resource.csv"
            candidate.write_bytes(b"new")
            destination.write_bytes(b"old")

            def validator(path: Path) -> bytes:
                payload = path.read_bytes()
                if path == destination:
                    raise ValueError("cold readback failed")
                return payload

            with self.assertRaises(ResourcePortabilityError) as caught:
                ResourceArtifactSaveService().publish(
                    candidate,
                    destination,
                    validator,
                    owner_commit=lambda _publication, _validation: None,
                )
            self.assertEqual(caught.exception.code, "RESOURCE.EXPORT.VALIDATION_FAILED")
            self.assertEqual(destination.read_bytes(), b"old")
            self.assertFalse(any(root.glob(".resource-lkg-*.tmp")))

    def test_first_publication_failure_removes_only_owned_candidate_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            candidate = root / ".candidate"
            destination = root / "resource.csv"
            candidate.write_bytes(b"new")

            def validator(path: Path) -> bytes:
                if path == destination:
                    raise ValueError("cold readback failed")
                return path.read_bytes()

            with self.assertRaises(ResourcePortabilityError):
                ResourceArtifactSaveService().publish(
                    candidate,
                    destination,
                    validator,
                    owner_commit=lambda _publication, _validation: None,
                )
            self.assertFalse(destination.exists())

    @unittest.skipUnless(os.name == "nt", "Windows retained-handle behavior")
    def test_owner_commit_runs_while_published_destination_handle_is_retained(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            candidate = root / ".candidate"
            destination = root / "resource.csv"
            replacement = root / "replacement.csv"
            candidate.write_bytes(b"new")
            destination.write_bytes(b"old")
            replacement.write_bytes(b"new")
            owner_called = False

            def commit_owner(_publication: object, _validation: object) -> None:
                nonlocal owner_called
                owner_called = True
                with self.assertRaises(PermissionError):
                    replacement.replace(destination)
                self.assertTrue(any(root.glob(".resource-lkg-*.tmp")))

            ResourceArtifactSaveService().publish(
                candidate,
                destination,
                lambda path: path.read_bytes(),
                owner_commit=commit_owner,
            )

            self.assertTrue(owner_called)
            self.assertFalse(any(root.glob(".resource-lkg-*.tmp")))
            replacement.replace(destination)

    def test_ambiguous_begin_publish_preserves_lkg_and_unknown_residue(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            candidate = root / ".candidate"
            destination = root / "resource.csv"
            candidate.write_bytes(b"new")
            destination.write_bytes(b"old")
            backend = compose_platform_file_backend(root)
            probe_root = backend.bind_root(root)
            probe_parent = backend.bind_parent(
                probe_root,
                platform_relative_path(destination.name),
            )
            parent_type = type(probe_parent)
            probe_parent.close()
            probe_root.close()
            real_begin = parent_type.begin_publish
            calls = 0

            def ambiguous_second_begin(
                instance: object,
                *args: object,
                **kwargs: object,
            ) -> object:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise PlatformFileError(
                        PlatformFileErrorCode.RECOVERY_REQUIRED,
                        retryable=True,
                    )
                return real_begin(instance, *args, **kwargs)

            with patch.object(
                parent_type,
                "begin_publish",
                new=ambiguous_second_begin,
            ):
                with self.assertRaises(ResourcePortabilityError) as caught:
                    ResourceArtifactSaveService(backend).publish(
                        candidate,
                        destination,
                        lambda path: path.read_bytes(),
                        owner_commit=lambda _publication, _validation: None,
                    )

            self.assertEqual(caught.exception.code, "RESOURCE.EXPORT.RECOVERY_REQUIRED")
            self.assertEqual(destination.read_bytes(), b"old")
            self.assertTrue(any(root.glob(".resource-lkg-*.tmp")))
            self.assertTrue(any(root.glob(".resource-candidate-*.tmp")))


if __name__ == "__main__":
    unittest.main()
