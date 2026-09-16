from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, TYPE_CHECKING, Protocol

from pydantic import BaseModel

if TYPE_CHECKING:
    from recap.dsl.query import BaseQuery


@dataclass(frozen=True, slots=True)
class ExportContext:
    query: BaseQuery
    items: Sequence[BaseModel]


class Exporter(Protocol):
    def export(
        self,
        context: ExportContext,
        destination: Path | IO | None,
    ) -> object: ...
