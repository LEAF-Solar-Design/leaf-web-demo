"""X07 role document v2 adds typed tracker INSERT selectors without rewriting v1 rows."""
import json

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, TypeAdapter


class TrackerRole(BaseModel):
    model_config = ConfigDict(extra="forbid")
    block: StrictStr = Field(min_length=1, max_length=512)
    layer: StrictStr = Field(min_length=1, max_length=512)
    tracker_type: StrictStr = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    modules_per_instance: StrictInt = Field(ge=1, le=1000000)


_TRACKERS = TypeAdapter(list[TrackerRole])


def validate_trackers(rows):
    if not isinstance(rows, list):
        raise ValueError("tracker roles require an array")
    seen = set()
    multipliers = {}
    for tracker in _TRACKERS.validate_python(rows):
        pair = (tracker.block, tracker.layer)
        if pair in seen:
            raise ValueError("tracker block and layer must be unique")
        seen.add(pair)
        if multipliers.setdefault(tracker.tracker_type, tracker.modules_per_instance) != tracker.modules_per_instance:
            raise ValueError("tracker type has conflicting multipliers")


def encode_roles(roles):
    document = roles
    if isinstance(roles, dict) and "trackers" in roles:
        validate_trackers(roles["trackers"])
        document = {"schema_version": 2, "roles": roles}
    return json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False)


def read_roles(document):
    if isinstance(document, dict) and "schema_version" in document:
        if (type(document["schema_version"]) is not int or document["schema_version"] != 2
                or set(document) != {"schema_version", "roles"}):
            raise ValueError("unknown role document schema")
        document = document["roles"]
    if isinstance(document, dict) and "trackers" in document:
        validate_trackers(document["trackers"])
    return document
