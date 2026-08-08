from dataclasses import dataclass
from typing import Any


@dataclass
class _ConnectionState:
    read_backend: Any
    write_backend: Any
    engine: Any = None
    sessionmaker: Any = None
    _active_views: int = 0
    closed: bool = False

    def acquire(self) -> None:
        if self.closed:
            raise RuntimeError("Connection state is closed")
        self._active_views += 1

    def release(self) -> None:
        if self.closed:
            return
        if self._active_views:
            self._active_views -= 1
        if self._active_views == 0:
            self.close()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if hasattr(self.read_backend, "close"):
            self.read_backend.close()
        if self.write_backend is not self.read_backend and hasattr(
            self.write_backend, "close"
        ):
            self.write_backend.close()
        if self.engine is not None:
            self.engine.dispose()
