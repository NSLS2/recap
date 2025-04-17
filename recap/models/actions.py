import sqlalchemy

from .base import Base
from .attribute import Attribute, AttributeValueMixin, action_type_attribute_association
from sqlalchemy import (
    Column,
    ForeignKey,
    Table,
)
from sqlalchemy.orm import relationship, mapped_column, Mapped, Session, validates
from sqlalchemy.ext.hybrid import hybrid_property
from typing import List, Optional, Set, Any
from uuid import uuid4, UUID


class Parameter(Base, AttributeValueMixin):
    __tablename__ = "parameter"
    uid: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    action_id: Mapped[UUID] = mapped_column(ForeignKey("action.uid"), nullable=False)
    action: Mapped["Action"] = relationship(back_populates="parameters")

    def __init__(self, *args, **kwargs):
        value = kwargs.pop("value", None)
        attribute = kwargs.get("attribute")
        super().__init__(*args, **kwargs)
        if value is None:
            value = attribute.default_value
        self.set_value(value)


class ActionType(Base):
    __tablename__ = "action_type"
    uid: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(unique=True, nullable=False)
    # parameter_type: Mapped["ParameterType"] =
    attributes: Mapped[List["Attribute"]] = relationship(
        back_populates="action_types", secondary=action_type_attribute_association
    )
    experiment_associations: Mapped[List["ExperimentActionOrder"]] = relationship(
        "ExperimentActionOrder", back_populates="action_type"
    )


class Action(Base):
    __tablename__ = "action"
    uid: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(unique=True, nullable=False)

    action_type_id: Mapped[UUID] = mapped_column(ForeignKey("action_type.uid"), nullable=False)
    action_type: Mapped["ActionType"] = relationship()

    source_container_id: Mapped[Optional[UUID]] = mapped_column(ForeignKey("container.uid"), nullable=True)
    source_container: Mapped["Container"] = relationship("Container", foreign_keys=[source_container_id])

    dest_container_id: Mapped[Optional[UUID]] = mapped_column(ForeignKey("container.uid"), nullable=True)
    dest_container: Mapped["Container"] = relationship("Container", foreign_keys=[dest_container_id])

    next_action_id: Mapped[UUID] = mapped_column(ForeignKey("action.uid"), nullable=True)
    next_actions: Mapped["Action"] = relationship("Action", foreign_keys=[next_action_id])

    prev_action_id: Mapped[UUID] = mapped_column(ForeignKey("action.uid"), nullable=True)
    prev_actions: Mapped["Action"] = relationship("Action", foreign_keys=[prev_action_id])

    # parameter_id: Mapped[UUID] = mapped_column(ForeignKey("parameter.uid"), nullable=False)
    parameters: Mapped[List["Parameter"]] = relationship(back_populates="action")

    def __init__(self, *args, **kwargs):
        action_type = kwargs.get("action_type", None)
        super().__init__(*args, **kwargs)

        if action_type:
            self._initialize_from_action_type(action_type)

    def _initialize_from_action_type(self, action_type: Optional[ActionType] = None):
        """
        Automatically initialize action from action_type
        - Only add parameters if not present
        """
        if not action_type:
            return

        for param in self.action_type.attributes:
            if not any(p.attribute.uid == param.uid for p in self.parameters):
                self.parameters.append(Parameter(attribute=param, value=None))
