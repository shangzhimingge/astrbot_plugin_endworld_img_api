"""Validation helpers shared by the plugin Page handlers."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class ConfigValidationError(ValueError):
    """Raised when a Page configuration payload is invalid."""

    def __init__(self, errors: dict[str, str]):
        self.errors = errors
        super().__init__("; ".join(f"{path}: {message}" for path, message in errors.items()))


def load_schema() -> dict[str, Any]:
    with (Path(__file__).resolve().parent / "_conf_schema.json").open(encoding="utf-8") as handle:
        schema = json.load(handle)
    if not isinstance(schema, dict):
        raise ValueError("configuration schema must be an object")
    return schema


def _check_exact_keys(value: dict[str, Any], allowed: set[str], path: str, errors: dict[str, str]) -> None:
    for key in sorted(set(value) - allowed):
        errors[f"{path}.{key}" if path else key] = "未知配置项"
    for key in sorted(allowed - set(value)):
        errors[f"{path}.{key}" if path else key] = "缺少配置项"


def _validate_scalar(value: Any, rule: dict[str, Any], path: str, errors: dict[str, str]) -> Any:
    kind = rule.get("type")
    if kind == "bool":
        if type(value) is not bool:
            errors[path] = "必须是布尔值"
            return None
        return value
    if kind == "int":
        if type(value) is not int:
            errors[path] = "必须是整数"
            return None
        minimum = rule.get("minimum")
        maximum = rule.get("maximum")
        if minimum is not None and value < minimum:
            errors[path] = f"不得小于 {minimum}"
        elif maximum is not None and value > maximum:
            errors[path] = f"不得大于 {maximum}"
        return value
    if kind == "string":
        if not isinstance(value, str):
            errors[path] = "必须是字符串"
            return None
        normalized = value.strip()
        options = rule.get("options")
        if options is not None and normalized not in options:
            errors[path] = "值不在允许范围内"
        return normalized
    if kind == "list":
        if not isinstance(value, list):
            errors[path] = "必须是列表"
            return None
        normalized = []
        for index, item in enumerate(value):
            item_path = f"{path}[{index}]"
            if not isinstance(item, (str, int)) or isinstance(item, bool):
                errors[item_path] = "必须是字符串或整数"
                continue
            text = str(item).strip()
            if text:
                normalized.append(text)
        return normalized
    errors[path] = f"不支持的配置类型: {kind}"
    return None


def _validate_sources(value: Any, rule: dict[str, Any], path: str, errors: dict[str, str]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        errors[path] = "必须是列表"
        return []
    if not value:
        errors[path] = "至少保留一个图源"
        return []
    template = rule.get("templates", {}).get("default_source", {})
    items = template.get("items", {})
    allowed = set(items) | {"__template_key"}
    normalized_sources: list[dict[str, Any]] = []
    for index, source in enumerate(value):
        source_path = f"{path}[{index}]"
        if not isinstance(source, dict):
            errors[source_path] = "图源必须是对象"
            continue
        _check_exact_keys(source, allowed, source_path, errors)
        normalized: dict[str, Any] = {"__template_key": "default_source"}
        for key, item_rule in items.items():
            if key in source:
                normalized[key] = _validate_scalar(source[key], item_rule, f"{source_path}.{key}", errors)
        if not normalized.get("name"):
            errors[f"{source_path}.name"] = "图源名称不得为空"
        if not normalized.get("keywords"):
            errors[f"{source_path}.keywords"] = "至少填写一个触发词"
        apis = normalized.get("apis") or []
        if not apis:
            errors[f"{source_path}.apis"] = "至少填写一个 API 地址"
        for api_index, raw_url in enumerate(apis):
            parsed = urlparse(raw_url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                errors[f"{source_path}.apis[{api_index}]"] = "必须是带主机名的 HTTP/HTTPS 地址"
        normalized_sources.append(normalized)
    return normalized_sources


def validate_and_normalize(candidate: Any, schema: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a deep normalized copy or raise field-addressable errors."""

    schema = schema or load_schema()
    if not isinstance(candidate, dict):
        raise ConfigValidationError({"config": "配置必须是对象"})
    errors: dict[str, str] = {}
    _check_exact_keys(candidate, set(schema), "", errors)
    normalized: dict[str, Any] = {}
    for key, rule in schema.items():
        if key not in candidate:
            continue
        if rule.get("type") == "template_list":
            normalized[key] = _validate_sources(candidate[key], rule, key, errors)
        else:
            normalized[key] = _validate_scalar(candidate[key], rule, key, errors)
    if errors:
        raise ConfigValidationError(errors)
    return copy.deepcopy(normalized)


def summarize_changes(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """Create a compact, JSON-safe description of configuration changes."""

    summary: list[dict[str, Any]] = []
    for key in sorted((set(before) | set(after)) - {"sources"}):
        old, new = before.get(key), after.get(key)
        if old != new:
            summary.append({"kind": "changed", "path": key, "before": old, "after": new})

    old_sources = before.get("sources", []) if isinstance(before.get("sources", []), list) else []
    new_sources = after.get("sources", []) if isinstance(after.get("sources", []), list) else []
    old_names = [str(item.get("name", "")) for item in old_sources if isinstance(item, dict)]
    new_names = [str(item.get("name", "")) for item in new_sources if isinstance(item, dict)]
    for name in old_names:
        if name not in new_names:
            summary.append({"kind": "source_removed", "path": "sources", "name": name})
    for name in new_names:
        if name not in old_names:
            summary.append({"kind": "source_added", "path": "sources", "name": name})
    if len(old_names) == len(new_names) and sorted(old_names) == sorted(new_names) and old_names != new_names:
        summary.append({"kind": "sources_reordered", "path": "sources", "before": old_names, "after": new_names})
    for index, (old, new) in enumerate(zip(old_sources, new_sources)):
        if isinstance(old, dict) and isinstance(new, dict) and old.get("name") == new.get("name"):
            for key in sorted((set(old) | set(new)) - {"__template_key"}):
                if old.get(key) != new.get(key):
                    summary.append(
                        {"kind": "changed", "path": f"sources[{index}].{key}", "before": old.get(key), "after": new.get(key)}
                    )
    return summary
