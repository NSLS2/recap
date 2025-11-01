from recap.db.process import Direction
from recap.schemas.common import CommonFields


class ResourceTypeSchema(CommonFields):
    name: str


class ResourceSlotSchema(CommonFields):
    name: str
    resource_type: ResourceTypeSchema
    direction: Direction
