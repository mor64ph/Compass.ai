"""Turn a Pydantic model into a strict JSON schema the Messages API accepts.

Anthropic structured outputs want a schema where every object node has an
explicit `required` list covering all properties and `additionalProperties:
false`. Pydantic's `model_json_schema()` gives neither, and it emits `$ref` /
`$defs` for nested models. This module dereferences and hardens the result.
"""

from __future__ import annotations

import copy
from typing import Any

from pydantic import BaseModel

# Schema *metadata* worth dropping: it bloats the payload and no provider uses
# it. Only ever stripped in keyword position - see `_MAP_VALUED_KEYWORDS`.
_STRIP_KEYS = {"title", "default", "examples"}

# Keywords whose value is a mapping of *names* to schemas. Their keys are
# user-chosen field names, not JSON Schema keywords, so they must never be
# filtered against `_STRIP_KEYS`.
#
# This distinction is the whole point: a field genuinely named `title` (as
# `LLMExperience.title` and `LLMStarStory.title` are) was previously deleted from
# the schema, because the strip ran over every dict key at every depth. The model
# then never saw the field, omitted it, and Pydantic rejected the response - on
# every provider.
_MAP_VALUED_KEYWORDS = {"properties", "$defs", "definitions", "patternProperties"}


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})
    resolved = _deref(raw, defs, seen=())
    return _harden(resolved)


def _deref(node: Any, defs: dict[str, Any], seen: tuple[str, ...]) -> Any:
    """Inline every `$ref`. Recursive models are rejected loudly rather than
    silently producing an infinite schema."""
    if isinstance(node, list):
        return [_deref(item, defs, seen) for item in node]
    if not isinstance(node, dict):
        return node

    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        name = ref.split("/")[-1]
        if name in seen:
            raise ValueError(
                f"Recursive model {name!r} cannot be used as a structured-output schema"
            )
        target = defs.get(name)
        if target is None:
            raise ValueError(f"Unresolvable $ref {ref!r}")
        merged = copy.deepcopy(target)
        # Sibling keys alongside a $ref (e.g. a description) win.
        for key, value in node.items():
            if key != "$ref":
                merged[key] = value
        return _deref(merged, defs, seen + (name,))

    return {key: _deref(value, defs, seen) for key, value in node.items()}


def _harden(node: Any) -> Any:
    if isinstance(node, list):
        return [_harden(item) for item in node]
    if not isinstance(node, dict):
        return node

    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in _MAP_VALUED_KEYWORDS and isinstance(value, dict):
            # Recurse into the schemas, leave the field names alone.
            out[key] = {name: _harden(sub) for name, sub in value.items()}
        elif key in _STRIP_KEYS:
            continue
        else:
            out[key] = _harden(value)

    # `anyOf` from an Optional field would produce a nullable union. The LLM
    # output models in app/schemas.py deliberately avoid Optional, so hitting
    # this means a schema regression - fail loudly instead of shipping a shape
    # the API may reject at runtime.
    if "anyOf" in out and not out.get("properties"):
        variants = [v for v in out["anyOf"] if v.get("type") != "null"]
        if len(variants) == 1:
            merged = variants[0]
            for key, value in out.items():
                if key != "anyOf":
                    merged.setdefault(key, value)
            return merged

    if out.get("type") == "object" or "properties" in out:
        props = out.setdefault("properties", {})
        out["required"] = list(props.keys())
        out["additionalProperties"] = False

    return out
