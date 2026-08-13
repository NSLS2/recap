from dataclasses import dataclass
from typing import Any

from recap.client.backend import ClientBackend


@dataclass
class _ConnectionState:
    backend: ClientBackend
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
        if self.engine is not None:
            self.engine.dispose()
