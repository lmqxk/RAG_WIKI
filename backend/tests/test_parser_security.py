"""验证 MinerU ZIP 解析产物的安全解压边界。"""

import io
import zipfile
from pathlib import Path

import pytest

from backend.parser import ParsingError, extract_zip_safely


def test_extract_zip_safely_rejects_path_traversal(tmp_path: Path) -> None:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("../outside.txt", "blocked")

    with zipfile.ZipFile(io.BytesIO(payload.getvalue())) as archive:
        with pytest.raises(ParsingError, match="非法解压路径"):
            extract_zip_safely(archive, tmp_path / "output")

    assert not (tmp_path / "outside.txt").exists()
