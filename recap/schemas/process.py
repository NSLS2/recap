"""Pydantic schemas for campaigns, process templates, and process runs.

This module defines the top-level provenance objects:

* :class:`CampaignSchema` — the root grouping for a set of related
  :class:`ProcessRunSchema` instances (corresponds to a beamtime, project,
  or experimental campaign).
* :class:`ProcessTemplateSchema` — the workflow blueprint that declares
  ordered steps and resource slots.
* :class:`ProcessRunSchema` — a concrete execution of a template, carrying
  live step parameter values, assigned resources, and provenance metadata.
* :class:`ProcessTemplateRef` and :class:`ProcessRunRef` — lightweight
  reference types used to avoid circular serialisation.
"""

import warnings
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import ConfigDict, PrivateAttr, field_validator

from recap.exceptions import UnloadedFieldError, UnloadedFieldWarning
from recap.schemas.common import SIMPLE_FIELD, CommonFields
from recap.schemas.resource import ResourceAssignmentSchema, ResourceSlotSchema
from recap.schemas.step import StepSchema, StepTemplateSchema


class ProcessTemplateRef(CommonFields):
    """Lightweight reference to a process template.

    Used inside :class:`ProcessRunRef` and similar contexts where the full
    :class:`ProcessTemplateSchema` (including all step and slot details) is
    not required.

    Attributes:
        name: Human-readable template name.
        version: Version string (e.g. ``"1.0"``).
    """

    name: Annotated[str, SIMPLE_FIELD]
    version: Annotated[str, SIMPLE_FIELD]


class ProcessTemplateSchema(CommonFields):
    """Blueprint for a workflow, defining its ordered steps and resource slots.

    A :class:`ProcessTemplateSchema` is created once and reused across
    multiple :class:`ProcessRunSchema` instances.  It declares:

    * An ordered set of :class:`~recap.schemas.step.StepTemplateSchema`
      instances that represent the phases of the workflow.
    * A list of :class:`~recap.schemas.resource.ResourceSlotSchema` entries
      that specify which resource types can be assigned to the process and
      in which direction (input / output).

    Attributes:
        name: Unique human-readable template name.
        version: Version string (e.g. ``"1.0"``).
        is_active: When ``False`` the template is retired and new runs cannot
            be created against it.
        step_templates: Ordered mapping of step name →
            :class:`~recap.schemas.step.StepTemplateSchema`.
        resource_slots: List of
            :class:`~recap.schemas.resource.ResourceSlotSchema` entries
            declaring the typed input/output slots.
    """

    name: Annotated[str, SIMPLE_FIELD]
    version: Annotated[str, SIMPLE_FIELD]
    is_active: Annotated[bool, SIMPLE_FIELD]
    step_templates: dict[str, StepTemplateSchema]
    resource_slots: list["ResourceSlotSchema"]


class ProcessRunRef(CommonFields):
    """Lightweight reference to a process run.

    Used in list views or parent-link contexts where the full
    :class:`ProcessRunSchema` (with steps and assigned resources) is not
    needed.

    Attributes:
        name: Display name of the run.
        description: Free-text description.
        campaign_id: UUID of the owning :class:`CampaignSchema`.
        template: Lightweight :class:`ProcessTemplateRef` identifying which
            template was used.
    """

    name: Annotated[str, SIMPLE_FIELD]
    description: Annotated[str, SIMPLE_FIELD]
    campaign_id: Annotated[UUID, SIMPLE_FIELD]
    template: ProcessTemplateRef


class ProcessRunSchema(CommonFields):
    """A concrete execution of a :class:`ProcessTemplateSchema`.

    A :class:`ProcessRunSchema` is the primary provenance record.  It links
    together:

    * The workflow that was executed (``template``).
    * The resources that were used (``assigned_resources``).
    * The parameter values captured at each step (``steps``).
    * The campaign it belongs to (``campaign_id``).

    Chain multiple process runs by using the output resource of one run as
    the input of the next, creating a queryable provenance graph.

    Attributes:
        name: Display name for this run (e.g. ``"Run 001"``).
        description: Free-text description of what this run represents.
        campaign_id: UUID of the owning :class:`CampaignSchema`.
        template: The :class:`ProcessTemplateSchema` this run instantiates.
        steps: Mapping of step name → :class:`~recap.schemas.step.StepSchema`
            with live parameter values.
        assigned_resources: Mapping of slot name →
            :class:`~recap.schemas.resource.ResourceAssignmentSchema`
            binding resources to their slots.  Keyed by
            :attr:`~recap.schemas.resource.ResourceSlotSchema.name` so that
            a specific assignment can be retrieved directly, e.g.
            ``run.assigned_resources["crystal_plate"]``.
    """

    name: Annotated[str, SIMPLE_FIELD]
    description: Annotated[str, SIMPLE_FIELD]
    campaign_id: Annotated[UUID, SIMPLE_FIELD]
    template: ProcessTemplateSchema
    steps: dict[str, StepSchema]
    assigned_resources: dict[str, ResourceAssignmentSchema]
    model_config = ConfigDict(arbitrary_types_allowed=True, from_attributes=True)
    _loaded_relations: dict[str, bool] = PrivateAttr(default_factory=dict)
    _on_unloaded: Literal["silent", "warn", "raise"] = PrivateAttr(default="warn")
    _warned_unloaded: set[str] = PrivateAttr(default_factory=set)

    @field_validator("assigned_resources", mode="before")
    @classmethod
    def _coerce_assigned_resources(cls, v):
        """Convert a list of assignments (from the ORM) into a slot-name-keyed dict."""
        if isinstance(v, list):
            return {item.slot.name: item for item in v}
        return v

    def set_loaded_relations(
        self,
        loaded_relations: dict[str, bool],
        *,
        on_unloaded: Literal["silent", "warn", "raise"] = "warn",
    ) -> "ProcessRunSchema":
        self._loaded_relations = loaded_relations
        self._on_unloaded = on_unloaded
        self._warned_unloaded = set()
        return self

    def _handle_unloaded(self, field_name: str, include_hint: str) -> None:
        if self._loaded_relations.get(field_name, True):
            return
        message = (
            f"'{field_name}' was not loaded for ProcessRunSchema; "
            f"use {include_hint} or load='full'."
        )
        if self._on_unloaded == "raise":
            raise UnloadedFieldError(message)
        if self._on_unloaded == "warn" and field_name not in self._warned_unloaded:
            warnings.warn(message, UnloadedFieldWarning, stacklevel=3)
            self._warned_unloaded.add(field_name)

    def __getattribute__(self, name: str):
        if name == "assigned_resources":
            self._handle_unloaded("assigned_resources", "include('resources')")
        elif name == "steps":
            self._handle_unloaded("steps", "include('steps')")
        return super().__getattribute__(name)


class CampaignSchema(CommonFields):
    """Top-level grouping of process runs for a single experimental campaign.

    A campaign corresponds to a discrete period or project of experimental
    work — for example, a synchrotron beamtime allocation or a drug-screening
    campaign.  All :class:`ProcessRunSchema` instances belong to exactly one
    campaign.

    Create a campaign via
    :meth:`~recap.client.base_client.RecapClient.create_campaign` and activate
    an existing one via
    :meth:`~recap.client.base_client.RecapClient.set_campaign` before creating
    process runs.

    Attributes:
        name: Human-readable campaign name.
        proposal: Proposal or project identifier (e.g. ``"MX-2026-001"``).
        saf: Safety Approval Form or equivalent authorisation reference.
            ``None`` when not applicable.
        meta_data: Arbitrary JSON-serialisable key/value pairs stored with
            the campaign.
        process_runs: List of :class:`ProcessRunSchema` instances that belong
            to this campaign.
    """

    name: Annotated[str, SIMPLE_FIELD]
    proposal: Annotated[str, SIMPLE_FIELD]
    saf: Annotated[str | None, SIMPLE_FIELD]
    meta_data: Annotated[dict[str, Any] | None, SIMPLE_FIELD]
    process_runs: list["ProcessRunSchema"]
