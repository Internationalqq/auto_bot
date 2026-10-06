"""Give waiting Gulya the next GUI turn after Ivan releases a position.

The existing browser_lock still owns all input. This OS-held waiter lock only
prevents Ivan taking another position ahead of a waiting mail check. Crashed
waiters release automatically; active browser ownership is never expired/stolen.
"""
import argparse
import json
from pathlib import Path
import time


def acquire_turn(lock, root, owner):
    import fcntl
    root = Path(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if owner != 'commercial':
        return lock.operation(root, 'acquire', owner)
    with (root/'gulya-waiter.lock').open('a+') as waiter:
        try:
            fcntl.flock(waiter, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'status':'busy', 'owner':'gulya', 'reason':'waiting_for_next_gui_turn'}
        return lock.operation(root, 'acquire', owner)


def wait_for_gulya(lock, root, timeout=900, now=time.monotonic, sleep=time.sleep):
    import fcntl
    root = Path(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not 0 < timeout <= 900:
        raise ValueError('Waiting is bounded to 900 seconds')
    with (root/'gulya-waiter.lock').open('a+') as waiter:
        try:
            fcntl.flock(waiter, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'status':'busy', 'owner':'gulya', 'reason':'another_gulya_waiter'}
        deadline = now() + timeout
        while True:
            result = lock.operation(root, 'acquire', 'gulya')
            if result['status'] != 'busy':
                return result
            remaining = deadline - now()
            if remaining <= 0:
                return dict(result, reason='gui_wait_timeout')
            sleep(min(2, remaining))


if __name__ == '__main__':
    import browser_lock
    parser = argparse.ArgumentParser()
    parser.add_argument('--timeout', type=float, default=900)
    args = parser.parse_args()
    print(json.dumps(wait_for_gulya(browser_lock, Path(__file__).parent/'state', args.timeout)))
