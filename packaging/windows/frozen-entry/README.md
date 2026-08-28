# Windows frozen-entry candidate inputs

This directory contains the Task 1.5 target contract and its materialized
candidate-input lock. The lock binds the selected build search roots, pinned
CPython/PyInstaller inputs, expected native closure, exact Python C API target,
patch ownership/apply protocol, and expected PE import surface. The applied
patch and resulting PE belong to the Task 1.6 realized-build lock.

The producer runs with CPython 3.14 x64 and `pefile==2024.8.26`. Supply the
absolute paths for the selected local materialization, then run:

```powershell
$Replay = @{
    AuditPython = '<candidate audit venv>\Scripts\python.exe'
    VisualStudioInstall = '<Visual Studio Build Tools installation>'
    VisualStudioLayout = '<Visual Studio offline layout>'
    VsWhere = '<Visual Studio Installer>\vswhere.exe'
    WindowsSdkRoot = '<Windows Kits>\10'
    WindowsSdkVersion = '10.0.26100.0'
    MsvcVersion = '14.44.35207'
    PythonRoot = '<pinned CPython 3.14.7 root>'
    PyInstallerSdist = '<inputs>\pyinstaller-6.22.2.tar.gz'
    PyInstallerSource = '<inputs>\pyinstaller-6.22.2'
    ProbeWorkRoot = '<new empty parent>\stock-probe-source'
    SupportVerifiedOn = '2026-08-27'
    OutputPath = '<existing output parent>\candidate-input-replay.json'
}

.\tools\replay_windows_frozen_entry_inputs.ps1 @Replay
```

The replay wrapper copies the pinned pristine PyInstaller source, rejects any
pre-existing build cache, records the complete copied bootloader build-driver
aggregate before Waf can create build outputs, sanitizes the compiler environment, invokes
`vcvarsall.bat` with the exact requested MSVC/SDK versions, verifies the
resolved `cl/link/rc/mt` paths, and runs
`bootloader/waf all --target-arch=64bit -j1`. It retains raw stdout/stderr in the
new probe work root and emits portable canonical build evidence covering the
actual copied-source and pre-build build-driver aggregates, tool identities, arguments, sanitized
environment and result import table. The resulting stock `runw.exe` is used
only to prove the selected compiler's exact import-table delta. Its timestamped
PE bytes are not candidate authority; Task 1.6 owns reproducible customized PE
bytes and the realized-build lock.

`MATERIALIZED` means that the candidate inputs and target contract are
internally consistent. W3 reapproval changes the Task 1.5 governance state;
Task 1.6 then realizes and tests the custom entry against the mandatory matrix.

## Task 1.6 terminal diagnostic replay

The custom-entry producer stops before candidate construction when a hard
assertion cannot satisfy the approved Task 1.5 contract. The diagnostic probe
does not grant authority and its loader notification cannot prove closure. It
records an observed load, pinned-binary disassembly, and pristine PyInstaller
source facts as separate evidence classes. The `MODULE` records are loader
notification paths followed by diagnostic path reopen identity/digest checks;
they are not retained backing-file handles and do not satisfy actual-module
reproof.

Run from the repository root with a new output directory:

```powershell
& '<candidate audit venv>\Scripts\python.exe' -B `
  .\tools\audit_windows_frozen_custom_entry.py `
  --repository-root $PWD `
  --candidate-lock .\packaging\windows\frozen-entry\candidate-input.lock.json `
  --matrix-contract .\packaging\windows\frozen-entry\custom-entry-matrix.json `
  --pyinstaller-source '<inputs>\pyinstaller-6.22.2' `
  --cpython-root '<pinned CPython 3.14.7 root>' `
  --vcvarsall '<Visual Studio Build Tools>\VC\Auxiliary\Build\vcvarsall.bat' `
  --msvc-version 14.44.35207 `
  --windows-sdk-version 10.0.26100.0 `
  --dumpbin '<Visual Studio Build Tools>\VC\Tools\MSVC\14.44.35207\bin\Hostx64\x64\dumpbin.exe' `
  --output '<ignored artifact root>\task1-6-custom-no-go-replay'

& '<candidate audit venv>\Scripts\python.exe' -B `
  .\tools\validate_windows_frozen_custom_entry_no_go.py `
  --repository-root $PWD `
  --evidence '<ignored artifact root>\task1-6-custom-no-go-replay\custom-entry-no-go.json' `
  --raw-artifact-root '<ignored artifact root>\task1-6-custom-no-go-replay' `
  --candidate-lock .\packaging\windows\frozen-entry\candidate-input.lock.json `
  --matrix-contract .\packaging\windows\frozen-entry\custom-entry-matrix.json `
  --schema .\packaging\windows\frozen-entry\custom-entry-no-go.schema.json `
  --cpython-root '<pinned CPython 3.14.7 root>' `
  --pyinstaller-source '<inputs>\pyinstaller-6.22.2' `
  --vcvarsall '<Visual Studio Build Tools>\VC\Auxiliary\Build\vcvarsall.bat' `
  --msvc-version 14.44.35207 `
  --windows-sdk-version 10.0.26100.0 `
  --dumpbin '<Visual Studio Build Tools>\VC\Tools\MSVC\14.44.35207\bin\Hostx64\x64\dumpbin.exe' `
  --run-negative-self-tests
```

`NO_GO` deliberately returns a non-zero producer status. The validator must
emit `VALID_NO_GO`. It independently rebuilds and reruns the probe, replays the
PE/IAT/disassembly and pristine-source facts, and verifies every raw sibling
against the current lock and scoped working-diff identity. Before trusting PE
or subprocess-derived facts, it verifies the executing Python/base-runtime,
loaded `pefile` source/METADATA, and the MSVC/SDK `bin`/`include`/`lib` input
aggregates actually reachable by the build. The final probe environment replaces the broad
`vcvarsall` search lists with the exact locked MSVC/SDK x64 `INCLUDE`/`LIB`
projection and clears `LIBPATH`; both producer and validator reject any later
out-of-scope search root. Strict JSON parsing rejects duplicate keys at every
nesting level in producer and validator semantic inputs, and the repository
schema is applied fail-closed. The negative self-tests cover producer candidate
lock/matrix duplicates, evidence duplicate-key last-wins, schema-invalid
self-consistent evidence, producer-TCB tampering, toolchain aggregate tampering,
and an out-of-scope compile include root. Two clean replays
compare `replay_projection_digest`; each run retains its own content digest for
raw compiler/probe outputs.
