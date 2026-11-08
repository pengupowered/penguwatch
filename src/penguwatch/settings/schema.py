"""What `GET /api/settings/schema` sends: the registry as the page needs it, with each setting's type, range,
choices and default read from its pydantic field, the list's item fields, the secret slots, the drivers and their
capabilities (the Data sources pickers) and the built-in themes (the theme choice). The same for every request of a
build (api/settings_models.SettingsSchema is its shape)."""

from typing import Any, Dict, List, Type

from pydantic import BaseModel
from pydantic.fields import FieldInfo

from penguwatch import __version__
from penguwatch.drivers import DRIVERS, OPTIONAL_ROLES
from penguwatch.drivers.base import ROLES
from penguwatch.settings import registry
from penguwatch.web import themes


def item_field(name: str, info: FieldInfo) -> Dict[str, Any]:
    """One field of a list item: {name, required, default, type, ...constraints}."""
    return {"name": name, "required": info.is_required(),
            "default": None if info.is_required() else _json(registry.field_default(info)),
            **registry.field_constraints(info)}


def model_fields(model: Type[BaseModel]) -> List[Dict[str, Any]]:
    return [item_field(name, info) for name, info in model.model_fields.items()]


def _json(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_json(v) for v in value]
    return value


def setting_spec(setting: registry.Setting) -> Dict[str, Any]:
    spec = {"key": setting.key, "section": setting.section, "group": setting.group, "label": setting.label,
            "effect": setting.effect, "unit": setting.unit, "live": setting.live, "env": setting.env,
            "advanced": setting.advanced, "export": setting.export, "order": setting.order,
            "choice_labels": dict(setting.choice_labels) if setting.choice_labels else None, "empty": setting.empty,
            **setting.constraints(), "default": _json(setting.default())}
    if spec["type"] == "theme":  # the choices are the theme folders found at run time, named on the page by name
        spec["choices"] = list(themes.builtin_ids())
        spec["choice_labels"] = {t.id: t.name for t in themes.builtin_themes()}
    return spec


def list_spec(spec: registry.ListSpec) -> Dict[str, Any]:
    return {"label": spec.label, "section": spec.section, "path": spec.path, "max_items": spec.max_items,
            "item_fields": model_fields(spec.item_model), "default_items": _json(spec.default())}


def drivers_spec() -> List[Dict[str, Any]]:
    return [{"name": name, "label": driver.label, "offers": sorted(c.value for c in driver.offers),
             "remote": bool(getattr(driver, "remote", True))} for name, driver in sorted(DRIVERS.items())]


def roles_spec() -> List[Dict[str, Any]]:
    return [{"role": role, "capability": capability.value, "multiple": role != "library",
             "optional": role in OPTIONAL_ROLES} for role, capability in ROLES.items()]


def build_schema() -> Dict[str, Any]:
    """The schema document (api/settings_models.SettingsSchema)."""
    return {
        "version": __version__,
        "sections": [{"id": s.id, "title": s.title, "order": s.order} for s in registry.SECTIONS],
        "settings": [setting_spec(s) for s in registry.SETTINGS],
        "lists": {name: list_spec(spec) for name, spec in registry.LISTS.items()},
        "secrets": [{"slot": s.slot, "label": s.label, "kind": s.kind, "section": s.section, "env": s.env,
                     "pattern": s.pattern} for s in registry.SECRET_SLOTS],
        "drivers": drivers_spec(),
        "roles": roles_spec(),
        "themes": [{"id": t.id, "name": t.name, "description": t.description} for t in themes.builtin_themes()],
    }
