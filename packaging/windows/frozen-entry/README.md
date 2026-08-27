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
