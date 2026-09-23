"""M2-5 (S3): ProjectManager tenant guard.

Destructive/lazy cross-tenant scans must be explicit:
- ``get``/``delete``/``list`` with ``tenant_id=None`` raise ValueError.
- Internal tools that genuinely need cross-tenant lookup use the
  explicit ``get_any``/``list_all``/``find_metas`` escape hatches.
"""
from __future__ import annotations

import pytest

from mini_cc.projects import ProjectManager


@pytest.fixture
def pm(tmp_path):
    m = ProjectManager(tmp_path / "projects")
    m.create(tenant_id="t1", project_id="alpha")
    m.create(tenant_id="t2", project_id="beta")
    return m


def test_get_without_tenant_raises(pm):
    with pytest.raises(ValueError, match="tenant_id"):
        pm.get("alpha")


def test_list_without_tenant_raises(pm):
    with pytest.raises(ValueError, match="tenant_id"):
        pm.list()


def test_delete_without_tenant_raises(pm):
    with pytest.raises(ValueError, match="tenant_id"):
        pm.delete("alpha")


def test_cross_tenant_get_is_keyerror(pm):
    with pytest.raises(KeyError):
        pm.get("alpha", tenant_id="t2")


def test_explicit_get_any_works_for_internal_tools(pm):
    assert pm.get_any("beta").meta.tenant_id == "t2"


def test_explicit_list_all_works_for_internal_tools(pm):
    ids = sorted(p.project_id for p in pm.list_all())
    assert ids == ["alpha", "beta"]
