"""Deployment profiles: the endpoint wiring, pinned.

The profile difference is configuration + topology, and the
configuration half is testable: cloud resolves to Reducto's
hosted API, vpc to the internal endpoint, and an explicit
REDUCTO_BASE_URL wins in both.
"""

from __future__ import annotations

import pytest

from reducto_ap_agent.profiles import CLOUD_BASE_URL, VPC_DEFAULT_BASE_URL, resolve_profile

PROFILE_ENV = ("DEPLOYMENT_PROFILE", "REDUCTO_BASE_URL")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in PROFILE_ENV:
        monkeypatch.delenv(name, raising=False)


def test_cloud_profile_is_the_hosted_api(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "cloud")
    profile = resolve_profile()
    assert profile.name == "cloud"
    assert profile.reducto_base_url == CLOUD_BASE_URL == "https://platform.reducto.ai"
    assert profile.engine_internal_only is False


def test_vpc_profile_is_the_internal_endpoint(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "vpc")
    profile = resolve_profile()
    assert profile.name == "vpc"
    assert profile.reducto_base_url == VPC_DEFAULT_BASE_URL == "http://reducto-engine:8080"
    assert profile.engine_internal_only is True
    assert profile.is_vpc


def test_explicit_base_url_wins_in_both_profiles(monkeypatch):
    monkeypatch.setenv("REDUCTO_BASE_URL", "http://engine.internal:9000")
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "vpc")
    assert resolve_profile().reducto_base_url == "http://engine.internal:9000"
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "cloud")
    assert resolve_profile().reducto_base_url == "http://engine.internal:9000"


def test_unknown_profile_is_an_error(monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_PROFILE", "moon")
    with pytest.raises(ValueError):
        resolve_profile()


def test_wiring_reducto_without_key_fails_loudly(monkeypatch):
    from reducto_ap_agent.errors import DocumentNotConfigured
    from reducto_ap_agent.wiring import build_document_port

    monkeypatch.setenv("DOCUMENT_BACKEND", "reducto")
    monkeypatch.delenv("REDUCTO_API_KEY", raising=False)
    with pytest.raises(DocumentNotConfigured):
        build_document_port()
