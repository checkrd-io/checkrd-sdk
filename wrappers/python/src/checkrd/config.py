"""Configuration loading for Checkrd."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Union

import yaml

from checkrd.exceptions import CheckrdInitError

if TYPE_CHECKING:
    from checkrd._types import Policy

_POLICY_FILE = "policy.yaml"


def _default_config_dir() -> Path:
    override = os.environ.get("CHECKRD_CONFIG_DIR")
    if override:
        return Path(override)
    return Path.home() / ".checkrd"


def _resolve_policy(
    policy: Union[str, Path, "Policy", dict[str, Any], None],
) -> str:
    if isinstance(policy, dict):
        return json.dumps(policy)

    if isinstance(policy, (str, Path)):
        path = Path(policy)
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise CheckrdInitError(f"Cannot read policy file {path}: {e}") from e
        try:
            parsed = yaml.safe_load(content)
        except yaml.YAMLError as e:
            raise CheckrdInitError(f"Invalid YAML in {path}: {e}") from e
        if not isinstance(parsed, dict):
            raise CheckrdInitError(
                f"Policy file {path} must contain a YAML mapping, got {type(parsed).__name__}"
            )
        return json.dumps(parsed)

    # None: try default location
    default_path = _default_config_dir() / _POLICY_FILE
    if not default_path.exists():
        raise CheckrdInitError(
            f"No policy file found at {default_path}. "
            "Pass a policy dict or path to wrap(), or create ~/.checkrd/policy.yaml."
        )
    try:
        content = default_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise CheckrdInitError(f"Cannot read policy file {default_path}: {e}") from e
    try:
        parsed = yaml.safe_load(content)
    except yaml.YAMLError as e:
        raise CheckrdInitError(f"Invalid YAML in {default_path}: {e}") from e
    if not isinstance(parsed, dict):
        raise CheckrdInitError(
            f"Policy file {default_path} must contain a YAML mapping, got {type(parsed).__name__}"
        )
    return json.dumps(parsed)


def load_config(
    policy: Union[str, Path, "Policy", dict[str, Any], None] = None,
) -> str:
    """Load policy configuration.

    Args:
        policy: A :class:`checkrd.Policy` TypedDict, a YAML/JSON string,
            a :class:`pathlib.Path`, a raw dict (for backward compat),
            or None to load ``~/.checkrd/policy.yaml``. The TypedDict
            and raw-dict cases are interchangeable at runtime — both
            serialize to the same JSON the WASM core ingests; the
            TypedDict variant just gives static checkers a chance to
            catch missing keys before runtime.

    Returns:
        Policy JSON string ready to pass to the WASM engine.

    Raises:
        CheckrdInitError: If the policy file is missing, unreadable, or malformed.
    """
    return _resolve_policy(policy)
