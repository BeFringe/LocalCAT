from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from platform_fs import compose_platform_file_backend
from platform_fs_contracts import BoundDirectoryAuthority, PlatformFileError, PlatformFileErrorCode
from resource_artifact_save import ResourceArtifactSaveService
from resource_package_contracts import ResourcePortabilityError
from resource_platform_io import platform_relative_path


class ResourceArtifactSaveTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "POSIX permits writes to a retained output")
    def test_same_inode_foreign_destination_is_preserved_before_rollback(self) -> None:
        for existing in (False, True):
            for foreign in (b"bad", b"foreign-observed"):
                with self.subTest(existing=existing, foreign=foreign):
                    with tempfile.TemporaryDirectory() as raw:
                        root = Path(raw).resolve()
                        candidate, destination = root / ".candidate", root / "resource.csv"
                        candidate.write_bytes(b"new")
                        if existing:
                            destination.write_bytes(b"old")
                        callbacks: list[str] = []
                        destination_inodes: list[int] = []

                        def validator(path: Path) -> bytes:
                            payload = path.read_bytes()
                            if path == destination:
                                destination_inodes.append(path.stat().st_ino)
                                path.write_bytes(foreign)
                                self.assertEqual(path.stat().st_ino, destination_inodes[0])
                                raise ValueError("cold readback failed after foreign write")
                            return payload

                        with self.assertRaises(ResourcePortabilityError) as caught:
                            ResourceArtifactSaveService().publish(
                                candidate, destination, validator,
                                owner_commit=lambda *_: callbacks.append("commit"),
                                owner_finalize=lambda: callbacks.append("finalize"),
                            )

                        self.assertEqual(caught.exception.code, "RESOURCE.EXPORT.RECOVERY_REQUIRED")
                        self.assertEqual(callbacks, [])
                        self.assertEqual(candidate.read_bytes(), b"new")
                        self.assertEqual(destination.read_bytes(), foreign)
                        self.assertEqual(destination.stat().st_ino, destination_inodes[0])
                        self.assertEqual(
                            [path.read_bytes() for path in root.glob(".resource-lkg-*.tmp")],
                            [b"old"] if existing else [],
                        )

    def test_changed_lkg_is_preserved_without_restoring_it_as_prior(self) -> None:
        for mutation, foreign in (
            ("same-inode-same-size", b"bad"),
            ("same-inode-new-size", b"foreign-lkg"),
            ("new-inode-same-bytes", b"old"),
        ):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve()
                candidate, destination = root / ".candidate", root / "resource.csv"
                candidate.write_bytes(b"new")
                destination.write_bytes(b"old")
                callbacks: list[str] = []
                destination_inodes: list[int] = []
                lkg_inodes: list[int] = []

                def validator(path: Path) -> bytes:
                    if path == destination:
                        destination_inodes.append(path.stat().st_ino)
                        lkg = next(root.glob(".resource-lkg-*.tmp"))
                        prior_inode = lkg.stat().st_ino
                        if mutation == "new-inode-same-bytes":
                            replacement = root / ".foreign-lkg"
                            replacement.write_bytes(foreign)
                            replacement.replace(lkg)
                            self.assertNotEqual(lkg.stat().st_ino, prior_inode)
                        else:
                            lkg.write_bytes(foreign)
                            self.assertEqual(lkg.stat().st_ino, prior_inode)
                        lkg_inodes.append(lkg.stat().st_ino)
                        raise ValueError("cold readback failed after LKG change")
                    return path.read_bytes()

                with self.assertRaises(ResourcePortabilityError) as caught:
                    ResourceArtifactSaveService().publish(
                        candidate, destination, validator,
                        owner_commit=lambda *_: callbacks.append("commit"),
                        owner_finalize=lambda: callbacks.append("finalize"),
                    )

                self.assertEqual(caught.exception.code, "RESOURCE.EXPORT.RECOVERY_REQUIRED")
                self.assertEqual(callbacks, [])
                self.assertEqual(candidate.read_bytes(), b"new")
                self.assertEqual(destination.read_bytes(), b"new")
                self.assertEqual(destination.stat().st_ino, destination_inodes[0])
                lkgs = list(root.glob(".resource-lkg-*.tmp"))
                self.assertEqual(len(lkgs), 1)
                self.assertEqual(lkgs[0].read_bytes(), foreign)
                self.assertEqual(lkgs[0].stat().st_ino, lkg_inodes[0])

    def test_lkg_publication_failure_cleans_its_distinct_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            candidate, destination = root / '.candidate', root / 'resource.csv'
            candidate.write_bytes(b'new')
            destination.write_bytes(b'old')
            real_publish = BoundDirectoryAuthority.begin_publish
            reached = []

            def publish(parent, value, name, **kwargs):
                if name.startswith('.resource-lkg-'):
                    reached.append(name)
                    raise PlatformFileError(PlatformFileErrorCode.PUBLISH_FAILED, retryable=True)
                return real_publish(parent, value, name, **kwargs)

            with patch.object(BoundDirectoryAuthority, 'begin_publish', publish):
                with self.assertRaises(ResourcePortabilityError) as caught:
                    ResourceArtifactSaveService().publish(candidate, destination,
                        lambda path: path.read_bytes(), owner_commit=lambda *_: self.fail('unpublished callback'))
            self.assertEqual(caught.exception.code, 'RESOURCE.EXPORT.PUBLICATION_FAILED')
            self.assertEqual(len(reached), 1)
            self.assertEqual(destination.read_bytes(), b'old')
            self.assertEqual(candidate.read_bytes(), b'new')
            self.assertFalse(any(root.glob('.resource-lkg*.tmp')))

    @unittest.skipUnless(os.name == 'posix', 'POSIX observable destination replacement')
    def test_foreign_destination_after_publication_is_preserved_with_prior_lkg(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            candidate, destination = root / '.candidate', root / 'resource.csv'
            candidate.write_bytes(b'new')
            destination.write_bytes(b'old')
            def validator(path):
                payload = path.read_bytes()
                if path == destination:
                    foreign = root / 'foreign'
                    foreign.write_bytes(b'foreign')
                    foreign.replace(destination)
                return payload
            with self.assertRaises(ResourcePortabilityError) as caught:
                ResourceArtifactSaveService().publish(candidate, destination, validator,
                    owner_commit=lambda *_: self.fail('foreign callback'))
            self.assertEqual(caught.exception.code, 'RESOURCE.EXPORT.RECOVERY_REQUIRED')
            self.assertEqual(destination.read_bytes(), b'foreign')
            self.assertEqual(candidate.read_bytes(), b'new')
            self.assertEqual([path.read_bytes() for path in root.glob('.resource-lkg-*.tmp')], [b'old'])

    def test_existing_destination_has_distinct_lkg_candidate_and_cold_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            candidate = root / ".candidate"
            destination = root / "resource.csv"
            candidate.write_bytes(b"new")
            destination.write_bytes(b"old")
            callbacks = []

            def commit(publication, validation):
                self.assertEqual(destination.read_bytes(), b"new")
                self.assertEqual(validation, b"new")
                self.assertEqual([p.read_bytes() for p in root.glob(".resource-lkg-*.tmp")], [b"old"])
                callbacks.append(publication)

            publication, validation = ResourceArtifactSaveService().publish(
                candidate, destination, lambda path: path.read_bytes(), owner_commit=commit,
            )
            self.assertEqual(callbacks, [publication])
            self.assertEqual(validation, b"new")
            self.assertEqual(destination.read_bytes(), b"new")
            self.assertEqual(candidate.read_bytes(), b"new")
            self.assertFalse(any(root.glob(".resource-lkg*.tmp")))

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
            callbacks: list[str] = []

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
                    owner_commit=lambda *_: callbacks.append("commit"),
                    owner_finalize=lambda: callbacks.append("finalize"),
                )
            self.assertEqual(caught.exception.code, "RESOURCE.EXPORT.VALIDATION_FAILED")
            self.assertEqual(callbacks, [])
            self.assertEqual(candidate.read_bytes(), b"new")
            self.assertEqual(destination.read_bytes(), b"old")
            self.assertFalse(any(root.glob(".resource-lkg-*.tmp")))

    def test_first_publication_failure_removes_only_owned_candidate_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            candidate = root / ".candidate"
            destination = root / "resource.csv"
            candidate.write_bytes(b"new")
            callbacks: list[str] = []

            def validator(path: Path) -> bytes:
                if path == destination:
                    raise ValueError("cold readback failed")
                return path.read_bytes()

            with self.assertRaises(ResourcePortabilityError) as caught:
                ResourceArtifactSaveService().publish(
                    candidate,
                    destination,
                    validator,
                    owner_commit=lambda *_: callbacks.append("commit"),
                    owner_finalize=lambda: callbacks.append("finalize"),
                )
            self.assertEqual(caught.exception.code, "RESOURCE.EXPORT.VALIDATION_FAILED")
            self.assertEqual(callbacks, [])
            self.assertEqual(candidate.read_bytes(), b"new")
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
