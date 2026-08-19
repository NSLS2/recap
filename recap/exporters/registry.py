from __future__ import annotations

from .protocol import Exporter


class ExporterRegistry:
    def __init__(self) -> None:
        self._exporters: dict[str, Exporter] = {}

    def register(self, name: str, exporter: Exporter) -> None:
        if name in self._exporters:
            raise ValueError(f"Exporter already registered for format {name!r}")
        self._exporters[name] = exporter

    def get(self, name: str) -> Exporter:
        try:
            return self._exporters[name]
        except KeyError as exc:
            raise KeyError(f"Unknown export format {name!r}") from exc


default_exporter_registry = ExporterRegistry()
