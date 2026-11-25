import hashlib
import importlib.util
import io
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "artifact.py"
spec = importlib.util.spec_from_file_location("deployment_artifact", SCRIPT)
assert spec is not None and spec.loader is not None
artifact = importlib.util.module_from_spec(spec)
spec.loader.exec_module(artifact)

URL = "https://productionresultssa7.blob.core.windows.net/archive.zip?sig=fixture"
IMAGE = b"fixture image"


@pytest.mark.parametrize(
    "url",
    [
        "http://productionresultssa7.blob.core.windows.net/archive.zip?sig=fixture",
        "https://productionresultssa7.blob.core.windows.net.example.test/archive.zip?sig=fixture",
        "https://127.0.0.1/archive.zip?sig=fixture",
        "https://productionresultssa7.blob.core.windows.net/archive.zip",
    ],
)
def test_download_only_accepts_github_artifact_origin(url):
    with pytest.raises(artifact.ArtifactError, match="invalid_download_origin"):
        artifact.check_url(url)


@pytest.mark.parametrize(
    ("member", "descriptor_changes", "expected"),
    [
        ("image.tar.gz", {}, None),
        ("../image.tar.gz", {}, "invalid_image_entry"),
        ("image.tar.gz", {"archive_sha256": "a" * 64}, "archive_digest_mismatch"),
        ("image.tar.gz", {"increment_archive_bytes": True}, "archive_size_mismatch"),
    ],
)
def test_verified_download_materializes_only_the_expected_archive(
    tmp_path, monkeypatch, member, descriptor_changes, expected
):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w") as bundle:
        bundle.writestr(member, IMAGE)
    body = buffer.getvalue()

    class Response(io.BytesIO):
        status = 200

    class Opener:
        def open(self, url, *, timeout):
            assert url == URL
            assert timeout > 0
            return Response(body)

    monkeypatch.setattr(artifact, "opener", Opener)
    desc = {
        "url": URL,
        "archive_bytes": len(body) + int(descriptor_changes.get("increment_archive_bytes", False)),
        "archive_sha256": hashlib.sha256(body).hexdigest(),
    }
    desc.update({key: value for key, value in descriptor_changes.items() if key in desc})
    path = tmp_path / "image.tar.gz"
    if expected:
        with pytest.raises(artifact.ArtifactError, match=expected):
            artifact.download(tmp_path, len(IMAGE), desc)
        assert not path.exists()
    else:
        artifact.download(tmp_path, len(IMAGE), desc)
        assert path.read_bytes() == IMAGE
