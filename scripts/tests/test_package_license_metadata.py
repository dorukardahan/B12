"""Regression tests for the PEP 639 package license contract."""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _pyproject() -> dict:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def test_setuptools_floor_supports_pep_639_license_metadata():
    build_requires = _pyproject()["build-system"]["requires"]

    assert "setuptools>=77.0.3" in build_requires


def test_project_license_uses_pep_639_fields():
    project = _pyproject()["project"]

    assert project["license"] == "MIT"
    assert project["license-files"] == ["LICENSE"]


def test_deprecated_license_classifier_is_removed():
    classifiers = _pyproject()["project"]["classifiers"]

    assert "License :: OSI Approved :: MIT License" not in classifiers
