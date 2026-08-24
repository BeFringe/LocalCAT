from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from resource_artifact_save import ResourceArtifactSaveService
from resource_package_contracts import ResourcePortabilityError


class ResourceArtifactSaveTests(unittest.TestCase):
    def test_symlink_and_hardlink_destinations_are_rejected_without_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            source = root / "existing.csv"
            source.write_bytes(b"old")
            for name, make_destination in (
                ("symlink.csv", lambda path: path.symlink_to(source)),
                ("hardlink.csv", lambda path: path.hardlink_to(source)),
            ):
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
                        )
                    self.assertEqual(
                        caught.exception.code,
                        "RESOURCE.EXPORT.DESTINATION_STALE",
                    )
                    self.assertEqual(source.read_bytes(), b"old")
                    self.assertEqual(candidate.read_bytes(), b"new")
                    destination.unlink()

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
                )
            self.assertEqual(
                caught.exception.code,
                "RESOURCE.EXPORT.DESTINATION_STALE",
            )
            self.assertEqual((moved / "resource.csv").read_bytes(), b"old")
            self.assertEqual((moved / ".candidate").read_bytes(), b"new")

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
                )
            self.assertEqual(caught.exception.code, "RESOURCE.EXPORT.VALIDATION_FAILED")
            self.assertEqual(destination.read_bytes(), b"old")
            self.assertFalse(any(root.glob(".*.lkg")))

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
                ResourceArtifactSaveService().publish(candidate, destination, validator)
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
