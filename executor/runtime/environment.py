"""The deployment environment label every executor log record carries.

WHY THIS EXISTS.  The `Leaf/InstantExecution` CloudWatch metrics are published
by CloudWatch Logs metric filters over the lines this package writes.  A metric
filter dimension value must be a SELECTOR such as `$.record.env`; AWS rejects a
literal with "dimension value must be a valid selector".  So a real `env`
dimension is possible only if the log RECORD carries an environment field.
Until this module existed no record did, which is why every metric in that
namespace was published without dimensions and a future production deployment
would have collided with staging's series inside one namespace.

The value is already available to the process: the staging task definitions set
`LEAF_INSTANT_ENV=staging` on both the executor and the control container.
Nothing put it in the log line.  This module is that missing step.

TWO RULES, and they solve two different failure modes.  Read them together,
because each one alone leaves a hole.

  1. THE FIELD IS ALWAYS PRESENT.  `resolve_environment_label` never returns
     None and never invites a caller to omit the key.  This is not politeness:
     a metric filter whose dimension selector finds no field does not publish
     the event under a different dimension, it does not publish the event AT
     ALL.  So a single record missing `env` is a silently dropped datapoint,
     and a deployment whose records all miss it is a metric that vanishes while
     its alarm sits in a steady green OK -- which is precisely the defect the
     whole instant-execution telemetry effort exists to remove.  A present
     sentinel keeps the pipeline observable; an absent key makes it disappear.

  2. A DEPLOYED EXECUTOR REFUSES TO START WITHOUT A REAL VALUE.  Rule 1 alone
     would let a misconfigured deployment publish every datapoint under
     `env="unset"`, which no alarm watches -- quiet in a different way.  So
     `require_environment_label` is called from the service entrypoint for any
     non-loopback listener, exactly as the runtime control secret, the TLS
     material and the JWKS trust bundle already are.  An unset variable then
     fails the container at boot, where ECS makes it impossible to miss,
     instead of failing a dimension nobody is looking at.

     The sentinel therefore cannot occur in a deployed environment.  It exists
     so a loopback development run still emits well-formed lines.

THE CHARACTER RULE IS LOAD-BEARING, not tidiness.  The executor writes JSON, so
it would tolerate almost anything, but the control plane publishes its half of
this namespace through a gunicorn access line that CloudWatch parses as
SPACE-DELIMITED fields.  A label containing a space would shift every field
after it and the heartbeat pattern would stop matching -- the same silent
metric loss as rule 1, arriving by a different route.  One shared rule keeps
both consumers safe, so this is deliberately stricter than either alone needs.
"""
from __future__ import annotations

import os
import re
from typing import Mapping

#: The container environment variable the task definitions already set.
ENVIRONMENT_VARIABLE = "LEAF_INSTANT_ENV"

#: Used only by a loopback development run; see rule 2 above.  It is a real
#: value rather than an omitted key on purpose: the key must always exist.
UNSET_ENVIRONMENT_LABEL = "unset"

#: No whitespace, and nothing that needs quoting in a space-delimited access
#: log line.  Bounded so one label cannot inflate every record toward the
#: 4096-byte atomic write limit the two emitters enforce.
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


class EnvironmentLabelError(ValueError):
    """The configured environment label cannot be published as a dimension."""


def _read(environ: Mapping[str, str] | None) -> str:
    source = os.environ if environ is None else environ
    return source.get(ENVIRONMENT_VARIABLE, "").strip()


def resolve_environment_label(environ: Mapping[str, str] | None = None) -> str:
    """Return the label to stamp on a record, never raising and never empty.

    This is the EMITTER's entry point, and it is total by design.  Telemetry
    must not be able to break the path it observes: `emit_runtime_event`
    already swallows every failure for that reason, and an emitter that could
    raise on a malformed label would hand a configuration mistake the power to
    fail an invocation.  A value that would corrupt the line is downgraded to
    the sentinel rather than propagated, so the record stays well-formed and
    the misconfiguration shows up as datapoints under `env="unset"`.

    The loud path is `require_environment_label`, called once at startup.
    """
    value = _read(environ)
    if not value or not _LABEL.fullmatch(value):
        return UNSET_ENVIRONMENT_LABEL
    return value


def require_environment_label(environ: Mapping[str, str] | None = None) -> str:
    """Return the label, or raise because this deployment cannot be monitored.

    Called from the service entrypoint for a non-loopback listener.  Refusing
    here converts an invisible telemetry hole into a container that will not
    boot, which is the only form of this failure an operator reliably sees.
    """
    value = _read(environ)
    if not value:
        raise EnvironmentLabelError(
            f"{ENVIRONMENT_VARIABLE} is required for a non-loopback executor: "
            "every Leaf/InstantExecution metric is dimensioned on it, and a "
            "record without it publishes no datapoint at all"
        )
    if not _LABEL.fullmatch(value):
        raise EnvironmentLabelError(
            f"{ENVIRONMENT_VARIABLE} must match {_LABEL.pattern} -- no spaces, "
            "because the control plane publishes its half of this namespace "
            "through a space-delimited access log line"
        )
    return value
