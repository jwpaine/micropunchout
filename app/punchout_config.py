"""Punchout settings: URL, field mappings and how the cart is returned.

Settings are layered. Defaults live here; a vendor and each of its buyer connections store only
their overrides as JSON objects. The settings for a punchout are

    defaults  ←  vendor overrides  ←  connection overrides

merged in that order, so a connection can change one mapping for one buyer without affecting the
vendor's other buyers. Nested objects merge key by key; anything else (lists, strings) replaces.
A mapping set to null is switched off.
"""

import json
import re
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.mapping import PoomRule, PosrRule

MAX_CONFIG_BYTES = 64 * 1024


class _Strict(BaseModel):
    # Unknown keys are errors, so a typo is rejected on save instead of being silently ignored.
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


def _check_base_url(value: str | None) -> str | None:
    if value is None:
        return None
    if not re.fullmatch(r"https?://[^\s/?#]+(/[^\s?#]*)?", value):
        raise ValueError("must be an http(s) URL like https://punchout.example.com")
    return value.rstrip("/")


class PunchoutSettings(_Strict):
    base_url: Annotated[str | None, AfterValidator(_check_base_url)] = Field(
        None,
        description="Public URL buyers reach this punchout at, e.g. a custom domain. "
        "Used in the punchout URL and StartPage links. Empty uses the app's own URL.",
    )


class ReturnSettings(_Strict):
    form_field: Literal["cxml-urlencoded", "cxml-base64"] = Field(
        "cxml-urlencoded", description="Form field the cart is posted in."
    )
    target: Literal["_self", "_top"] = Field(
        "_self", description="Use _top to break out of an iframe when returning the cart."
    )


# --- setup request mapping (posr) -------------------------------------------

# Fields the app itself uses. Any other name is kept on the session and the shopper's account.
POSR_FIELD_DESCRIPTIONS = {
    "user.external_id": "Shopper's ID; identifies the shopper account (per buyer connection). Required.",
    "user.email": "Shopper's email.",
    "user.name": "Shopper's display name.",
    "ship_to.id": "Ship-to address ID.",
}
POSR_TARGET = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*")

DEFAULT_POSR: dict[str, dict[str, Any]] = {
    "user.external_id": {
        "from": ["extrinsic:UniqueName", "extrinsic:UserId", "extrinsic:User", "extrinsic:UserEmail", "contact:email"],
        "required": True,
    },
    "user.email": {"from": ["extrinsic:UserEmail", "contact:email"]},
    "user.name": {
        "from": [
            "extrinsic:UserPrintableName",
            "extrinsic:FirstName + extrinsic:LastName",
            "contact:name",
            "extrinsic:UniqueName",
            "extrinsic:UserEmail",
            "contact:email",
        ]
    },
    "ship_to.id": {"from": ["shipto:address_id"]},
}

# --- cart mapping (poom) ----------------------------------------------------

POOM_FIELD_DESCRIPTIONS = {
    "item.description": "<Description>. Falls back to the product name if empty.",
    "item.supplier_part_aux_id": "<SupplierPartAuxiliaryID>. Comes back on the buyer's purchase order.",
    "item.classification": "<Classification> code.",
    "item.classification_domain": "<Classification domain=…>. UNSPSC if empty.",
    "item.manufacturer_part_id": "<ManufacturerPartID>.",
    "item.manufacturer_name": "<ManufacturerName>.",
    "item.lead_time": "<LeadTime> in days. Ignored unless it's a whole number.",
}
POOM_EXTRINSIC_PREFIX = "item.extrinsic."
POOM_EXTRINSIC_NAME = re.compile(r"[A-Za-z_][\w .-]{0,99}")

DEFAULT_POOM: dict[str, dict[str, Any]] = {
    "item.description": {"from": ["product:name"]},
    "item.classification": {"from": ["product:unspsc"]},
    "item.classification_domain": {"from": ["value:UNSPSC"]},
    "item.manufacturer_part_id": {"from": ["product:manufacturer_part_id"]},
    "item.manufacturer_name": {"from": ["product:manufacturer_name"]},
    "item.lead_time": {"from": ["product:lead_time_days"]},
}


class PunchoutConfig(_Strict):
    punchout: PunchoutSettings = Field(default_factory=PunchoutSettings)
    posr: dict[str, PosrRule | None] = Field(default_factory=lambda: dict(DEFAULT_POSR), validate_default=True)
    poom: dict[str, PoomRule | None] = Field(default_factory=lambda: dict(DEFAULT_POOM), validate_default=True)
    return_: ReturnSettings = Field(default_factory=ReturnSettings, alias="return")

    @field_validator("posr")
    @classmethod
    def _check_posr(cls, rules: dict[str, PosrRule | None]) -> dict[str, PosrRule | None]:
        for target in rules:
            if len(target) > 100 or not POSR_TARGET.fullmatch(target):
                raise ValueError(f"{target!r} is not a valid field name; use lowercase names like user.email")
        if rules.get("user.external_id") is None:
            raise ValueError("user.external_id is required to identify the shopper and can't be switched off")
        return rules

    @field_validator("poom")
    @classmethod
    def _check_poom(cls, rules: dict[str, PoomRule | None]) -> dict[str, PoomRule | None]:
        for target in rules:
            if target in POOM_FIELD_DESCRIPTIONS:
                continue
            if target.startswith(POOM_EXTRINSIC_PREFIX) and POOM_EXTRINSIC_NAME.fullmatch(
                target[len(POOM_EXTRINSIC_PREFIX):]
            ):
                continue
            raise ValueError(
                f"{target!r} is not a cart field; use one of {', '.join(POOM_FIELD_DESCRIPTIONS)} "
                f"or item.extrinsic.<Name>"
            )
        return rules

    def posr_rules(self) -> dict[str, PosrRule]:
        return {k: v for k, v in self.posr.items() if v is not None}

    def poom_rules(self) -> dict[str, PoomRule]:
        return {k: v for k, v in self.poom.items() if v is not None}


DEFAULTS: dict[str, Any] = PunchoutConfig().model_dump(by_alias=True)


class ConfigError(ValueError):
    pass


# The admin edits settings one section at a time. Each section owns some top-level keys.
SECTIONS: dict[str, dict[str, Any]] = {
    "general": {"title": "General", "keys": ("punchout", "return")},
    "posr": {"title": "Setup request mapping (POSR)", "keys": ("posr",)},
    "poom": {"title": "Cart mapping (POOM)", "keys": ("poom",)},
}


def section_overrides(overrides: dict[str, Any], section: str) -> dict[str, Any]:
    """The part of a layer's overrides that a section edits, as shown in its editor.

    General shows {"punchout": ..., "return": ...}; the mapping sections show just their rules.
    """
    keys = SECTIONS[section]["keys"]
    if section == "general":
        return {k: overrides[k] for k in keys if k in overrides}
    return overrides.get(keys[0], {})


def section_defaults(section: str) -> dict[str, Any]:
    return section_overrides(DEFAULTS, section)


def apply_section(overrides: dict[str, Any], section: str, text: str, *below: dict[str, Any]) -> dict[str, Any]:
    """Replace one section of a layer's overrides with the JSON typed into its editor.

    Returns the layer's full new overrides, validated together with the layers ``below`` it.
    Raises ConfigError with readable messages.
    """
    edited = _load_object(text)
    keys = SECTIONS[section]["keys"]
    if section == "general":
        if stray := [k for k in edited if k not in keys]:
            raise ConfigError(
                f"{', '.join(stray)}: not a general setting. "
                "Field mappings are edited on the Setup request and Cart tabs."
            )
        replacement = edited
    else:
        replacement = {keys[0]: edited} if edited else {}
    updated = {k: v for k, v in overrides.items() if k not in keys} | replacement
    return parse_overrides(json.dumps(updated), *below)


def merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge ``overrides`` onto ``base``. Nested objects merge; anything else replaces."""
    out = dict(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = value
    return out


def merged(*layers: dict[str, Any]) -> dict[str, Any]:
    result = DEFAULTS
    for layer in layers:
        result = merge(result, layer)
    return result


def effective(*layers: dict[str, Any]) -> PunchoutConfig:
    """The settings in effect: defaults, then each layer's overrides (vendor, then connection)."""
    return PunchoutConfig.model_validate(merged(*layers))


def parse_overrides(text: str, *below: dict[str, Any]) -> dict[str, Any]:
    """Parse and validate overrides typed into the admin. Raises ConfigError with readable messages.

    ``below`` are the layers underneath (e.g. the vendor's overrides when editing a connection),
    so the check is on the settings that would actually be in effect.
    """
    overrides = _load_object(text)
    try:
        effective(*below, overrides)
    except ValidationError as e:
        raise ConfigError("; ".join(_describe(err) for err in e.errors())) from None
    return overrides


def _load_object(text: str) -> dict[str, Any]:
    if len(text.encode()) > MAX_CONFIG_BYTES:
        raise ConfigError(f"Settings must be under {MAX_CONFIG_BYTES // 1024} KB.")
    text = text.strip() or "{}"
    try:
        value = json.loads(text)
    except json.JSONDecodeError as e:
        raise ConfigError(f"Not valid JSON: {e.msg} (line {e.lineno}, column {e.colno})") from None
    if not isinstance(value, dict):
        raise ConfigError("Settings must be a JSON object, e.g. {...}")
    return value


def _describe(err: dict[str, Any]) -> str:
    path = ".".join(str(p) for p in err["loc"] if p not in ("PosrRule", "PoomRule"))
    msg = err["msg"].removeprefix("Value error, ")
    if err["type"] == "extra_forbidden":
        return f"{path}: unknown setting"
    return f"{path}: {msg}"


def to_json(overrides: dict[str, Any]) -> str:
    return json.dumps(overrides, indent=2, sort_keys=True)


def explain(*layers: tuple[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Every setting with its value in effect and which layer it came from.

    ``layers`` are (name, overrides) pairs from lowest to highest, e.g. ("vendor", {...}),
    ("connection", {...}). Mapping rules are listed one row per field.
    """
    values = effective(*(overrides for _, overrides in layers)).model_dump(by_alias=True)

    def source(section: str, key: str) -> str:
        for name, overrides in reversed(layers):
            if isinstance(overrides.get(section), dict) and key in overrides[section]:
                return name
        return "default"

    rows = []
    for section, model in (("punchout", PunchoutSettings), ("return", ReturnSettings)):
        for name, field in model.model_fields.items():
            rows.append({
                "section": "general", "path": f"{section}.{name}", "value": values[section][name],
                "source": source(section, name), "description": field.description or "",
            })
    for section, descriptions in (("posr", POSR_FIELD_DESCRIPTIONS), ("poom", POOM_FIELD_DESCRIPTIONS)):
        for key, rule in values[section].items():
            if rule is not None:
                rule = {k: v for k, v in rule.items() if v not in (None, [], False, " ") or k == "from"}
            rows.append({
                "section": section, "field": key,
                "path": f"{section}.{key}", "value": rule, "source": source(section, key),
                "description": descriptions.get(key, "Line <Extrinsic>." if section == "poom" else "Kept on the session and shopper account."),
            })
    return rows
