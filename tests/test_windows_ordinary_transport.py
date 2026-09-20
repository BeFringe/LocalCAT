"""Real process ownership independently of the later packaged integration."""
import subprocess
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from frozen_worker_transport import _collect_child, _parse_worker_header, _worker_header, run_ordinary_child


class OrdinaryTransportTests(unittest.TestCase):
    def child(self, code):
        return subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_success_waits_for_exit_and_closes_endpoints(self):
        child = self.child("import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()[::-1])")
        self.assertEqual(_collect_child(child, b"abc", 10.0), (b"cba", b""))
        self.assertEqual(child.returncode, 0)
        self.assertTrue(all(stream.closed for stream in (child.stdin, child.stdout, child.stderr)))

    def test_timeout_reaps_process_and_closes_endpoints(self):
        child = self.child("import time; time.sleep(30)")
        with self.assertRaises(subprocess.TimeoutExpired):
            _collect_child(child, b"", 0.2)
        self.assertIsNotNone(child.poll())
        self.assertTrue(child.stdout.closed)

    def test_cancellation_reaps_process_and_closes_endpoints(self):
        child = self.child("import time; time.sleep(30)")
        deadline = time.monotonic() + 0.2
        def check_live():
            if time.monotonic() >= deadline:
                raise RuntimeError("revoked")
        with self.assertRaisesRegex(RuntimeError, "revoked"):
            _collect_child(child, b"", 10.0, check_live)
        self.assertIsNotNone(child.poll())
        self.assertTrue(child.stdout.closed)

    def test_handshake_has_both_candidate_and_request(self):
        header = _worker_header("a" * 64, "b" * 32)
        self.assertEqual(_parse_worker_header(header)["request_id"], "b" * 32)
        for bad in (b"{}", header[:-2], b"null", b"{\"protocol\":\"wrong\"}"):
            with self.subTest(bad=bad), self.assertRaises(OSError):
                _parse_worker_header(bad)

    def test_parent_rejects_wrong_candidate_request_and_truncated_result(self):
        # Source-process fault injection covers the pure reply association.
        # It is deliberately not counted as packaged runtime evidence.
        candidate = SimpleNamespace(executable=Path(sys.executable).resolve(), candidate_id="a" * 64)
        popen = subprocess.Popen
        replies = (_worker_header("c" * 64, "b" * 32) + b"{}",
                   _worker_header("a" * 64, "c" * 32) + b"{}",
                   b"{\n{}", _worker_header("a" * 64, "b" * 32)[:-1])
        for reply in replies:
            children = []
            def spawn(_command, **options):
                child = popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read(); sys.stdout.buffer.write(" + repr(reply) + ")"], **options)
                children.append(child)
                return child
            with self.subTest(reply=reply), \
                 patch("frozen_candidate.running_candidate", return_value=candidate), \
                 patch("frozen_worker_transport.uuid4", return_value=SimpleNamespace(hex="b" * 32)), \
                 patch("frozen_worker_transport.subprocess.Popen", side_effect=spawn), \
                 patch.dict("os.environ", {"SYSTEMROOT": "C:/Windows"}):
                with self.assertRaises(OSError):
                    run_ordinary_child((str(candidate.executable), "--localcat-tm-worker", "migration"), b"{}", 10.0)
            self.assertEqual(children[0].returncode, 0)
            self.assertTrue(all(stream.closed for stream in (children[0].stdin, children[0].stdout, children[0].stderr)))


if __name__ == "__main__":
    unittest.main()
