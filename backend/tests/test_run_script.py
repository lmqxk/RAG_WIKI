"""验证项目启动脚本：本地 rerank URL 才自动拉起 rerank 服务。"""

import importlib.util
from pathlib import Path


def load_run_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "run.py"
    spec = importlib.util.spec_from_file_location("rag_run_script", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_local_rerank_enabled_only_for_local_rerank_url() -> None:
    run = load_run_module()

    assert run.local_rerank_enabled("http://127.0.0.1:8011/rerank")
    assert run.local_rerank_enabled("http://localhost:8011/rerank")
    assert not run.local_rerank_enabled("https://api.example.com/v1/rerank")
    assert not run.local_rerank_enabled("")
    assert not run.local_rerank_enabled(None)
