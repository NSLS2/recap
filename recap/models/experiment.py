from recap.models.actions import ActionType
from .base import Base
from .attribute import Attribute, AttributeValueMixin, action_type_attribute_association
from sqlalchemy import (
    Column,
    ForeignKey,
    Table,
)
from sqlalchemy.orm import relationship, mapped_column, Mapped, Session, validates
from sqlalchemy.ext.associationproxy import association_proxy
from sqlalchemy.orm.exc import NoResultFound
from typing import List, Optional, Set, Any
from uuid import uuid4, UUID


class ExperimentActionOrder(Base):
    __tablename__ = "experiment_action_order"
    uuid: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    experiment_type_id: Mapped[UUID] = mapped_column(ForeignKey("experiment_type.uid"), primary_key=True)
    action_type_id: Mapped[UUID] = mapped_column(ForeignKey("action_type.uid"), primary_key=True)

    order_index: Mapped[int] = mapped_column(nullable=False)

    experiment_type: Mapped["ExperimentType"] = relationship(
        "ExperimentType", back_populates="action_associations"
    )
    action_type: Mapped["ActionType"] = relationship("ActionType", back_populates="experiment_associations")


class ExperimentType(Base):
    __tablename__ = "experiment_type"
    uid: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    name: Mapped[str] = mapped_column(unique=True, nullable=False)

    action_associations: Mapped[List[ExperimentActionOrder]] = relationship(
        "ExperimentActionOrder",
        back_populates="experiment_type",
        cascade="all, delete-orphan",
        order_by="ExperimentActionOrder.order_index",
    )

    action_types = association_proxy("action_associations", "action_type")

    def add_action_type(
        self, session: Session, action_type: ActionType, order_index: Optional[int] = None
    ) -> None:
        try:
            start_action_type = session.query(ActionType).filter_by(name="Start").one()
        except NoResultFound:
            start_action_type = ActionType(name="Start")
            session.add(start_action_type)
            session.commit()
            start_action_type = session.query(ActionType).filter_by(name="Start").one()

        try:
            end_action_type = session.query(ActionType).filter_by(name="End").one()
        except NoResultFound:
            end_action_type = ActionType(name="End")
            session.add(end_action_type)
            session.commit()
            end_action_type = session.query(ActionType).filter_by(name="End").one()

        if len(self.action_types) == 0:
            action_type_index = 1 if not order_index else order_index
            self.action_associations.extend(
                [
                    ExperimentActionOrder(order_index=0, action_type=start_action_type),
                    ExperimentActionOrder(order_index=action_type_index, action_type=action_type),
                    ExperimentActionOrder(order_index=action_type_index + 1, action_type=end_action_type),
                ]
            )
        else:
            end_action_type_association = self.action_associations[-1]
            self.action_associations.append(
                ExperimentActionOrder(
                    order_index=len(self.action_types) - 1 if not order_index else order_index,
                    action_type=action_type,
                )
            )
            non_end_orders = [
                assoc.order_index for assoc in self.action_associations if assoc.action_type.name != "End"
            ]
            end_action_type_association.order_index = max(non_end_orders) + 1

        session.commit()


class Experiment(Base):
    __tablename__ = "experiment"
    uid: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)

    name: Mapped[str] = mapped_column(unique=True, nullable=False)
    description: Mapped[str] = mapped_column(unique=False, nullable=False)

    action_id: Mapped[Optional[UUID]] = mapped_column(ForeignKey("action.uid"), nullable=True)
    actions: Mapped["Action"] = relationship("Action", foreign_keys=[action_id])
