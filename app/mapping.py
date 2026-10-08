"""Field mapping rules: where a value comes from and how it's cleaned up.

A rule lists sources tried in order; the first that produces a value wins:

    {"from": ["extrinsic:UserEmail", "contact:email"], "transform": ["lower"], "required": true}

A source can combine parts with " + " (joined with the rule's ``separator``, a space by default):

    "extrinsic:FirstName + extrinsic:LastName"

``value:<text>`` is a constant. The other source kinds depend on the direction:
setup request (PosrRule) or cart sent back (PoomRule).
"""

import re
from collections.abc import Callable, Mapping
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

PART_SEPARATOR = " + "

TRANSFORM = re.compile(r"lower|upper|trim|truncate:[1-9]\d{0,4}")

POSR_SOURCE = re.compile(
    r"extrinsic:\S.*"
    r"|contact:(name|email)"
    r"|shipto:(address_id|name|street|city|state|postal_code|country)"
    r"|from:(domain|identity)"
    r"|value:.*"
)
POSR_SOURCE_HELP = (
    "extrinsic:<Name>, contact:name|email, shipto:address_id|name|street|city|state|postal_code|country, "
    "from:domain|identity or value:<text>"
)

PRODUCT_FIELDS = (
    "sku", "name", "description", "unit_price", "currency", "uom", "unspsc",
    "manufacturer_name", "manufacturer_part_id", "lead_time_days",
)
POOM_SOURCE = re.compile(rf"product:({'|'.join(PRODUCT_FIELDS)})|session:[a-z][a-z0-9_.]*|value:.*")
POOM_SOURCE_HELP = "product:<field>, session:<mapped setup field> or value:<text>"


class _Rule(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    source_pattern: ClassVar[re.Pattern]
    source_help: ClassVar[str]

    from_: list[str] = Field(alias="from", min_length=1, max_length=10)
    separator: str = Field(" ", max_length=10)
    map: dict[str, str] | None = None
    transform: list[str] = Field(default_factory=list, max_length=5)
    default: str | None = None
    required: bool = False

    @field_validator("from_")
    @classmethod
    def _check_sources(cls, sources: list[str]) -> list[str]:
        for source in sources:
            for part in source.split(PART_SEPARATOR):
                if not cls.source_pattern.fullmatch(part.strip()):
                    raise ValueError(f"{source!r} is not a valid source; use {cls.source_help}")
        return sources

    @field_validator("transform")
    @classmethod
    def _check_transforms(cls, transforms: list[str]) -> list[str]:
        for t in transforms:
            if not TRANSFORM.fullmatch(t):
                raise ValueError(f"{t!r} is not a valid transform; use lower, upper, trim or truncate:<n>")
        return transforms


class PosrRule(_Rule):
    """Maps a value out of the PunchOutSetupRequest."""

    source_pattern = POSR_SOURCE
    source_help = POSR_SOURCE_HELP


class PoomRule(_Rule):
    """Maps a value into each item of the PunchOutOrderMessage."""

    source_pattern = POOM_SOURCE
    source_help = POOM_SOURCE_HELP


def _transform(name: str, value: str) -> str:
    if name == "lower":
        return value.lower()
    if name == "upper":
        return value.upper()
    if name == "trim":
        return value.strip()
    if name.startswith("truncate:"):
        return value[: int(name.split(":", 1)[1])].rstrip()
    return value


def resolve(rule: _Rule, lookup: Callable[[str], str]) -> str:
    """Apply one rule. ``lookup`` resolves a single source part like "extrinsic:UserEmail"."""
    value = ""
    for source in rule.from_:
        parts = []
        for part in source.split(PART_SEPARATOR):
            part = part.strip()
            parts.append(part[len("value:"):] if part.startswith("value:") else lookup(part))
        value = rule.separator.join(p for p in parts if p)
        if value:
            break
    if value and rule.map:
        value = rule.map.get(value, value)  # values not in the table pass through unchanged
    for name in rule.transform:
        value = _transform(name, value)
    if not value and rule.default is not None:
        value = rule.default
    return value


def map_fields(rules: Mapping[str, _Rule], lookup: Callable[[str], str]) -> tuple[dict[str, str], list[str]]:
    """Apply every rule. Returns the values and the names of required fields that came out empty."""
    values = {target: resolve(rule, lookup) for target, rule in rules.items()}
    missing = [target for target, rule in rules.items() if rule.required and not values[target]]
    return values, missing
