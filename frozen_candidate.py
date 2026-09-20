"""Build and loading facts for the ordinary PyInstaller distribution.

The installed distribution and OS are trusted. This is not a signature, a
source loader, or a proof of the boot chain. Core interprets compatibility;
this module keeps source digests separate from readable bundled data.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import sys

INPUT_RECORD = "localcat-owner-inputs.json"
CANDIDATE_RECORD = "localcat-candidate.json"


class CandidateError(RuntimeError):
    """The installed layout does not match its build record."""


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _relative(value: str) -> str:
    if (not isinstance(value, str) or not value or "\\" in value or ":" in value
            or any(ord(c) < 32 for c in value)
            or PurePosixPath(value).is_absolute()
            or any(p in ("", ".", "..") or p.endswith((".", " ")) for p in value.split("/"))):
        raise CandidateError("invalid bundle-relative input")
    return value


def _inventory(root: Path) -> dict[str, dict[str, object]]:
    return {
        path.relative_to(root).as_posix(): {"size": path.stat().st_size, "sha256": _sha(path.read_bytes())}
        for path in sorted(root.rglob("*"))
        if path.is_file() and path != root / CANDIDATE_RECORD
    }


def write_candidate_record(root: Path, build_record: dict) -> None:
    """Seal actual collected output after stock PyInstaller has finished."""
    payload = _inventory(root)
    body = {"schema": "localcat-ordinary-candidate-v1", "payload": payload, "build": build_record}
    body["candidate_id"] = _sha(canonical_json(payload))
    (root / CANDIDATE_RECORD).write_bytes(canonical_json(body))


@dataclass(frozen=True)
class OrdinaryCandidate:
    executable: Path
    bundle_root: Path
    candidate_id: str
    core_execution_digest: str
    _payload: dict
    _inputs: dict

    def _verify_file(self, relative: str) -> bytes:
        relative = _relative(relative)
        expected = self._payload.get(relative)
        if expected is None:
            raise CandidateError("file absent from candidate record")
        try:
            content = (self.executable.parent / relative).read_bytes()
        except OSError as exc:
            raise CandidateError("candidate file unavailable") from exc
        if len(content) != expected["size"] or _sha(content) != expected["sha256"]:
            raise CandidateError("candidate file differs from build output")
        return content

    def recheck_entry(self) -> None:
        # Entry-time check, not a recursive scan on each query.
        self._verify_file(self.executable.name)
        self._verify_file("_internal/" + INPUT_RECORD)
        for relative in self._inputs.get("runtime_files", []):
            self._verify_file(relative)

    def read_data(self, relative: str) -> bytes:
        relative = _relative(relative)
        if relative not in self._inputs["data_ids"]:
            raise CandidateError("input is not declared readable data")
        content = self._verify_file("_internal/" + relative)
        if _sha(content) != self._inputs["input_digests"].get(relative):
            raise CandidateError("data differs from its build input")
        return content

    def input_digest(self, relative: str) -> str:
        relative = _relative(relative)
        if relative in self._inputs["data_ids"]:
            return _sha(self.read_data(relative))
        try:
            return self._inputs["input_digests"][relative]
        except KeyError as exc:
            raise CandidateError("input absent from build record") from exc

    def is_data(self, relative: str) -> bool:
        return _relative(relative) in self._inputs["data_ids"]

    def execution_facts(self) -> dict:
        return json.loads(canonical_json(self._inputs["core_execution"]))

    def require_module(self, module) -> None:
        """Check only a consumed owner module's installed loading association."""
        spec = getattr(module, "__spec__", None)
        origin = getattr(module, "__file__", None)
        if (sys.modules.get(module.__name__) is not module or spec is None
                or spec.loader is not getattr(module, "__loader__", None)
                or spec.origin != origin or not isinstance(origin, str)
                or not Path(origin).resolve().is_relative_to(self.bundle_root)):
            raise CandidateError("owner module loaded outside the current candidate")
        relative = module.__name__.replace(".", "/") + ".py"
        if relative not in self._inputs["input_digests"]:
            raise CandidateError("owner module absent from declared inputs")


def load_candidate(executable: Path, *, verify_payload: bool = False) -> OrdinaryCandidate:
    executable = executable.resolve()
    try:
        record = json.loads((executable.parent / CANDIDATE_RECORD).read_bytes())
        identity = record.pop("candidate_id")
        if record["schema"] != "localcat-ordinary-candidate-v1" or _sha(canonical_json(record["payload"])) != identity:
            raise CandidateError("invalid candidate record")
        inputs = json.loads((executable.parent / "_internal" / INPUT_RECORD).read_bytes())
        if inputs["schema"] != "localcat-ordinary-inputs-v1":
            raise CandidateError("unsupported input record")
        for relative, digest in inputs["input_digests"].items():
            _relative(relative)
            if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise CandidateError("invalid input digest")
        if len(set(inputs["data_ids"])) != len(inputs["data_ids"]):
            raise CandidateError("duplicate data input")
        for relative in inputs["data_ids"]:
            if _relative(relative) not in inputs["input_digests"]:
                raise CandidateError("data input missing its digest")
        candidate = OrdinaryCandidate(executable, executable.parent / "_internal", identity,
                                      _sha(canonical_json(inputs["core_execution"])), record["payload"], inputs)
        candidate.recheck_entry()
        if verify_payload and _inventory(executable.parent) != record["payload"]:
            raise CandidateError("collected payload inventory differs")
        return candidate
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CandidateError("cannot load ordinary candidate") from exc


def running_candidate() -> OrdinaryCandidate:
    """Only the actual bundled process may compose production Core inputs."""
    if not getattr(sys, "frozen", False):
        raise CandidateError("ordinary composition requires a packaged process")
    candidate = load_candidate(Path(sys.executable))
    if Path(getattr(sys, "_MEIPASS", "")).resolve() != candidate.bundle_root:
        raise CandidateError("unexpected PyInstaller bundle location")
    if not Path(__file__).resolve().is_relative_to(candidate.bundle_root):
        raise CandidateError("entry imported outside the installed distribution")
    return candidate
