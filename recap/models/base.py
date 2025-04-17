from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.orm import relationship, mapped_column, Mapped, Session, validates
import sqlalchemy
from sqlalchemy import (
    Column,
    ForeignKey,
    Table,
)
from uuid import uuid4, UUID
from typing import List, Optional, Set, Any


class Base(DeclarativeBase):
    pass
