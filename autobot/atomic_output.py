"""Publish generated files only after a complete successful write."""
from pathlib import Path
import os
import tempfile
import threading
import time
import errno
from contextlib import contextmanager

_locks_guard = threading.Lock()
_locks = {}


@contextmanager
def output_lock(destination: Path, *, timeout: float = 20):
    """Serialize read/merge/write across threads and processes; OS releases on exit."""
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with _locks_guard:
        thread_lock = _locks.setdefault(str(destination), threading.Lock())
    if not thread_lock.acquire(timeout=timeout):
        raise TimeoutError('Отчёт занят другой операцией')
    handle = None
    acquired = False
    try:
        handle = open(str(destination) + '.lock', 'a+b')
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b'0')
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == 'nt':
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as error:
                if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError('Отчёт занят другим процессом')
                time.sleep(0.05)
        yield
    finally:
        try:
            if handle is not None:
                try:
                    if acquired:
                        if os.name == 'nt':
                            import msvcrt
                            handle.seek(0)
                            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                finally:
                    handle.close()
        finally:
            thread_lock.release()


def write_excel(frame, destination: Path) -> None:
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix='.autobot-', suffix='.xlsx', dir=destination.parent)
    os.close(handle)
    temporary = Path(name)
    try:
        frame.to_excel(temporary, index=False)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
