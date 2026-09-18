"""Bind reproduced build bytes and realized PE/API facts, not W3 acceptance.

The caller supplies the clean builder's external release/commit/input anchors.
This consumes two independently executed clean builds; it cannot establish
that history merely from two directories or grant loading/execution approval.
"""
from __future__ import annotations

import argparse
import importlib.metadata
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.build_windows_frozen_spike import verify_packaged_pe
from tools.prepare_windows_frozen_entry_inputs import _runtime_native_closure
from tools.probe_windows_frozen_custom_runw import byte_fact, canonical, checked_bytes, load_candidate
from tools.windows_frozen_api_binding import extract_binding
from tools.windows_frozen_release import verify_release_binding, _no_reparse_ancestors
from tools.verify_windows_frozen_payload import inventory

CLASSIFICATION = 'REPRODUCED_BUILD_FACTS_NOT_W3_ACCEPTANCE'
CODE = ('windows_frozen_realized_build.py', 'build_windows_frozen_spike.py',
        'prepare_windows_frozen_entry_inputs.py', 'probe_windows_frozen_custom_runw.py',
        'audit_w3_stock_bootloader.py', 'audit_windows_frozen_custom_entry.py',
        'windows_frozen_release.py', 'windows_frozen_api_binding.py',
        'windows_frozen_manifest.py', 'windows_frozen_packaging.py',
        'windows_frozen_patch_series.py', 'verify_windows_frozen_payload.py')


class RealizedBuildError(ValueError):
    pass


def compare_dist_bytes(first, second, members):
    if first.samefile(second):
        raise RealizedBuildError('two distinct build trees required')
    for name in members:
        a, b = first / name, second / name
        if a.samefile(b):
            raise RealizedBuildError('build trees share a member identity: ' + name)
        if a.read_bytes() != b.read_bytes():
            raise RealizedBuildError('two builds differ in raw bytes: ' + name)


def check_native_projection(actual, expected):
    if canonical(actual) != canonical(expected):
        raise RealizedBuildError('realized native dependency inventory differs from candidate')


def realize(args):
    for name in ('first_build', 'second_build', 'first_release', 'second_release'):
        setattr(args, name, _no_reparse_ancestors(Path(getattr(args, name)).absolute()))
    builds = (args.first_build, args.second_build)
    dists = tuple(p / 'packaging/dist/localcat-spike' for p in builds)
    releases = (args.first_release, args.second_release)
    release_bytes = tuple(p.read_bytes() for p in releases)
    if builds[0].samefile(builds[1]) or releases[0].samefile(releases[1]):
        raise RealizedBuildError('two independently recorded builds required')
    if release_bytes[0] != release_bytes[1]:
        raise RealizedBuildError('two releases differ')
    external = dict(expected_release_sha256=args.expected_release_sha256,
                    expected_repository_commit=args.expected_commit,
                    expected_candidate_input_digest=args.expected_candidate_digest)
    bound = [verify_release_binding(data, dist, **external) for data, dist in zip(release_bytes, dists)]
    candidate_path = ROOT / 'packaging/windows/frozen-entry/candidate-input.lock.json'
    candidate_bytes = candidate_path.read_bytes()
    candidate = load_candidate(candidate_path)
    if candidate['candidate_input_digest'] != args.expected_candidate_digest:
        raise RealizedBuildError('external candidate mismatch')
    import pefile
    parser_lock = candidate['evidence_producer']['pefile']
    checked_bytes(Path(pefile.__file__), parser_lock['source'])
    metadata = importlib.metadata.distribution('pefile').read_text('METADATA')
    if (pefile.__version__ != parser_lock['version'] or metadata is None or
            byte_fact(metadata.encode()) != {k:parser_lock['distribution_metadata'][k] for k in ('bytes', 'sha256')}):
        raise RealizedBuildError('unlocked PE parser metadata')
    def observer():
        return {**{'tools/' + n: byte_fact((ROOT/'tools'/n).read_bytes()) for n in CODE},
                'pefile':byte_fact(Path(pefile.__file__).read_bytes()),
                'pefile-METADATA':byte_fact(importlib.metadata.distribution('pefile').read_text('METADATA').encode()),
                'producer-python':byte_fact(Path(sys.executable).read_bytes())}
    before = observer()
    trees = tuple(inventory(dist) for dist in dists)
    if trees[0] != trees[1]:
        raise RealizedBuildError('distribution layouts differ')
    compare_dist_bytes(*dists, bound[0]['dist'])
    # These records were independently produced by the externally anchored clean
    # orchestrator. Re-read their actual bytes; never trust diagnostic.json flags.
    build_inputs = []
    for build, binding in zip(builds, bound):
        actual = {name:byte_fact(checked_bytes(_no_reparse_ancestors(build/name), fact))
                  for name, fact in binding['build_evidence'].items()}
        build_inputs.append(actual)
    if build_inputs[0] != build_inputs[1]:
        raise RealizedBuildError('prepared build evidence differs')
    for dist in dists:
        verify_packaged_pe(dist/'localcat-spike.exe', candidate)
    native = _runtime_native_closure(dists[0]/'_internal')
    check_native_projection(native, candidate['runtime']['cpython']['native_closure'])
    api = extract_binding((dists[0]/'localcat-spike.exe').read_bytes(),
                          (dists[0]/'_internal/python314.dll').read_bytes(),
                          candidate['entry_contract']['python_c_api'])
    for index, (release, dist, original) in enumerate(zip(releases, dists, release_bytes)):
        if release.read_bytes() != original or inventory(dist) != trees[index]:
            raise RealizedBuildError('build changed during realization')
        verify_release_binding(original, dist, **external)
        for name, fact in build_inputs[index].items():
            checked_bytes(builds[index]/name, fact)
    if candidate_path.read_bytes() != candidate_bytes or observer() != before:
        raise RealizedBuildError('candidate or observer changed')
    result = {'schema':'localcat.windows-frozen-realized-build.v1', 'classification':CLASSIFICATION,
        'external_anchors':external, 'candidate_lock':byte_fact(candidate_bytes),
        'executable':bound[0]['executable'],
        'dist':bound[0]['dist'], 'prelink':bound[0]['prelink'], 'runtime_manifest':bound[0]['runtime_manifest'],
        'applied_sources_digest':bound[0]['applied_sources_digest'], 'build_evidence':build_inputs[0],
        'realized_native_inventory':native, 'realized_python_api':api,
        'declared_dynamic_contract_not_execution_evidence':candidate['entry_contract']['dynamic_loader'],
        'source_context':bound[0]['source_context'], 'observer':before,
        'limitations':['Two clean executions are an external builder fact, not inferred from directories.',
            'Static/API/build facts do not substitute E0-E11 loading/execution evidence.']}
    output_parent = _no_reparse_ancestors(ROOT/'artifacts/windows')
    output = Path(tempfile.mkdtemp(prefix='realized-build-', dir=output_parent))
    (output/'realized-build.lock.json').write_bytes(canonical(result))
    (output/'invocation.json').write_bytes(canonical({'argv':sys.argv, 'cwd':str(Path.cwd()),
        'first_build':str(builds[0]), 'second_build':str(builds[1]),
        'first_release':str(releases[0]), 'second_release':str(releases[1])}))
    return output, result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('first-build', 'second-build', 'first-release', 'second-release'):
        parser.add_argument('--'+name, type=Path, required=True)
    for name in ('expected-release-sha256', 'expected-commit', 'expected-candidate-digest'):
        parser.add_argument('--'+name, required=True)
    directory, result = realize(parser.parse_args(argv))
    print(canonical({'classification':CLASSIFICATION, 'evidence':str(directory),
                     'lock_sha256':byte_fact(canonical(result))['sha256']}).decode())
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
