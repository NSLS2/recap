from dataclasses import dataclass, field
from threading import RLock
from typing import Any

from recap.client.backend import ClientBackend


@dataclass
class _ConnectionState:
    backend: ClientBackend
    engine: Any = None
    sessionmaker: Any = None
    _active_views: int = 0
    closed: bool = False
    _engine_disposed: bool = field(default=False, init=False, repr=False)
    _lifecycle_lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def acquire(self) -> None:
        with self._lifecycle_lock:
            if self.closed:
                raise RuntimeError("Connection state is closed")
            self._active_views += 1

    def release(self) -> None:
        with self._lifecycle_lock:
            if self.closed:
                return
            if self._active_views:
                self._active_views -= 1
            if self._active_views == 0:
                self.close()

    def close(self) -> None:
        with self._lifecycle_lock:
            if self.closed:
                return
            self.backend.close()
            if self.engine is not None and not self._engine_disposed:
                self.engine.dispose()
                self._engine_disposed = True
            self.closed = True
