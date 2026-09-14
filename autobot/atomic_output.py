"""Publish generated files only after a complete successful write."""
from pathlib import Path
import os
import tempfile


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
