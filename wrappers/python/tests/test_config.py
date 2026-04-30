"""Tests for checkrd.config."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from checkrd.config import load_config
from checkrd.exceptions import CheckrdInitError


class TestPolicyFromDict:
    def test_dict_serialized_to_json(self) -> None:
        policy = {"agent": "test", "default": "allow", "rules": []}
        policy_json = load_config(policy=policy)
        assert json.loads(policy_json) == policy

    def test_nested_dict(self) -> None:
        policy = {
            "agent": "test",
            "default": "deny",
            "rules": [{"name": "r1", "allow": {"method": ["GET"], "url": "example.com/*"}}],
        }
        policy_json = load_config(policy=policy)
        parsed = json.loads(policy_json)
        assert parsed["rules"][0]["name"] == "r1"


class TestPolicyFromFile:
    def test_yaml_file(self, tmp_path: Path) -> None:
        policy_file = tmp_path / "policy.yaml"
        policy_file.write_text("agent: test\ndefault: allow\nrules: []\n")
        policy_json = load_config(policy=policy_file)
        parsed = json.loads(policy_json)
        assert parsed["agent"] == "test"

    def test_string_path(self, tmp_path: Path) -> None:
        policy_file = tmp_path / "policy.yaml"
        policy_file.write_text("agent: test\ndefault: deny\nrules: []\n")
        policy_json = load_config(policy=str(policy_file))
        assert json.loads(policy_json)["default"] == "deny"

    def test_missing_file_raises(self) -> None:
        with pytest.raises(CheckrdInitError, match="Cannot read"):
            load_config(policy="/nonexistent/policy.yaml")

    def test_invalid_yaml_raises(self, tmp_path: Path) -> None:
        bad_file = tmp_path / "bad.yaml"
        bad_file.write_text(":\n  - :\n    invalid: [")
        with pytest.raises(CheckrdInitError, match="Invalid YAML"):
            load_config(policy=bad_file)

    def test_empty_yaml_raises(self, tmp_path: Path) -> None:
        empty_file = tmp_path / "empty.yaml"
        empty_file.write_text("")
        with pytest.raises(CheckrdInitError, match="YAML mapping"):
            load_config(policy=empty_file)

    def test_yaml_list_raises(self, tmp_path: Path) -> None:
        list_file = tmp_path / "list.yaml"
        list_file.write_text("- item1\n- item2\n")
        with pytest.raises(CheckrdInitError, match="YAML mapping"):
            load_config(policy=list_file)


class TestPolicyDefault:
    def test_missing_default_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CHECKRD_CONFIG_DIR", str(tmp_path))
        with pytest.raises(CheckrdInitError, match="No policy file found"):
            load_config()

    def test_reads_from_default_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CHECKRD_CONFIG_DIR", str(tmp_path))
        (tmp_path / "policy.yaml").write_text("agent: a\ndefault: allow\nrules: []\n")
        policy_json = load_config()
        assert json.loads(policy_json)["agent"] == "a"
