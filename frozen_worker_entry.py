"""Child dispatch after the native entry selected a fixed worker."""
from contextlib import ExitStack
import io
import os


def serve_selected_worker(kind: str, authority: object) -> int:
    from tm_gate_inputs import _compose_frozen_input_owner, _require_native_frozen_producer
    from tm_benchmark_worker import _WorkerPipes
    import _winapi
    import msvcrt

    return _serve_selected_worker(kind, authority=authority)


def serve_ordinary_worker(kind: str) -> int:
    return _serve_selected_worker(kind)


def _serve_selected_worker(kind: str, *, authority: object | None = None) -> int:
    from tm_gate_inputs import _compose_frozen_input_owner, _compose_ordinary_input_owner, _require_native_frozen_producer
    from tm_benchmark_worker import _WorkerPipes
    import _winapi
    import msvcrt

    if authority is not None:
        _require_native_frozen_producer(authority)
    if kind not in ("migration", "query"):
        raise ValueError("unknown frozen worker")
    incoming = _winapi.GetStdHandle(_winapi.STD_INPUT_HANDLE)
    outgoing = _winapi.GetStdHandle(_winapi.STD_OUTPUT_HANDLE)
    if incoming in (0, -1, None) or outgoing in (0, -1, None) or incoming == outgoing:
        raise OSError("worker directional handles are unavailable")
    if _winapi.GetFileType(incoming) != _winapi.FILE_TYPE_PIPE or _winapi.GetFileType(outgoing) != _winapi.FILE_TYPE_PIPE:
        raise OSError("worker endpoints must be inherited pipes")
    with ExitStack() as stack:
        request = stack.enter_context(io.open(msvcrt.open_osfhandle(incoming, os.O_RDONLY | os.O_BINARY), "rb"))
        result = stack.enter_context(io.open(msvcrt.open_osfhandle(outgoing, os.O_WRONLY | os.O_BINARY), "wb"))
        def diagnostic(error):
            from frozen_ordinary_entry import _diagnostic, _failure_event
            _diagnostic(_failure_event(kind, "ORDINARY.WORKER_FAILED", error))
        pipes = _WorkerPipes(request, result, diagnostic=diagnostic if authority is None else None)
        owner = _compose_frozen_input_owner(authority) if authority is not None else _compose_ordinary_input_owner()
        stack.callback(owner.close)
        session = stack.enter_context(owner.open_session())
        if authority is None:
            from frozen_worker_transport import _parse_worker_header
            header_line = request.readline(512)
            header = _parse_worker_header(header_line)
            if header["candidate_id"] != session._ordinary_candidate().candidate_id:
                raise OSError("worker candidate mismatch")
            result.write(header_line)
            result.flush()
        if kind == "migration":
            from tm_benchmark_process import _serve_worker
        else:
            from tm_benchmark_query_process import _serve_worker
        return _serve_worker(pipes, session)
