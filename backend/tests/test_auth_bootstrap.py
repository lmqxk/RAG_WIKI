"""验证认证引导创建与 JWT 配置约束。"""

from pathlib import Path

import pytest

from backend.config import Settings
from backend.db import Database
from backend.main import bootstrap_authentication
from backend.repository import Repository


def test_bootstrap_admin_creates_only_once(tmp_path: Path) -> None:
    database = Database(tmp_path / "rag.db")
    database.initialize()
    repository = Repository(database)
    settings = Settings(
        data_dir=tmp_path,
        mineru_model_dir=tmp_path / "models",
        auth_enabled=True,
        auth_jwt_secret="test-secret",
        auth_bootstrap_username="admin",
        auth_bootstrap_password="initial-password",
    )

    bootstrap_authentication(settings, repository)
    first = repository.get_user_by_username("admin")
    bootstrap_authentication(settings, repository)
    second = repository.get_user_by_username("admin")

    assert first is not None
    assert second is not None
    assert first["id"] == second["id"]


def test_enabled_auth_requires_jwt_secret(tmp_path: Path) -> None:
    database = Database(tmp_path / "rag.db")
    database.initialize()
    settings = Settings(data_dir=tmp_path, mineru_model_dir=tmp_path / "models", auth_enabled=True)

    with pytest.raises(RuntimeError, match="RAG_AUTH_JWT_SECRET"):
        bootstrap_authentication(settings, Repository(database))


def test_bootstrap_admin_is_member_of_default_organization(tmp_path: Path) -> None:
    database = Database(tmp_path / "rag.db")
    database.initialize()
    repository = Repository(database)
    repository.ensure_bootstrap_admin("admin", "initial-password")
    user = repository.get_user_by_username("admin")

    assert user is not None
    membership = repository.get_membership("default-org", str(user["id"]))
    assert membership == {
        "organization_id": "default-org",
        "user_id": user["id"],
        "role": "admin",
    }
