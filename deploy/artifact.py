"""Pass a verified GitHub artifact to the deployment host without exposing the token."""

import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

REPO = "LonelyFellas/tsz-agent"
FILENAME = "image.tar.gz"
BLOCK = 1024 * 1024


class ArtifactError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ArtifactError(code)


def opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def check_url(value: object) -> str:
    if not isinstance(value, str):
        raise ArtifactError("invalid_download_origin")
    url = urllib.parse.urlsplit(value)
    require(
        url.scheme == "https"
        and bool(re.fullmatch(r"productionresultssa[0-9]+\.blob\.core\.windows\.net", url.netloc))
        and bool(url.path and url.query and not url.fragment),
        "invalid_download_origin",
    )
    return value


def check_archive(archive: dict[str, Any], image_bytes: int) -> None:
    size = archive["archive_bytes"]
    sha256 = archive["archive_sha256"]
    require(
        type(size) is int
        and 0 < image_bytes <= size <= image_bytes + BLOCK
        and isinstance(sha256, str)
        and bool(re.fullmatch(r"[0-9a-f]{64}", sha256)),
        "invalid_archive_metadata",
    )


def locate(artifact_id: str, run_id: int, image_bytes: int) -> dict[str, Any]:
    require(bool(re.fullmatch(r"[1-9][0-9]*", artifact_id)) and run_id > 0, "invalid_artifact_id")
    endpoint = f"https://api.github.com/repos/{REPO}/actions/artifacts/{artifact_id}"
    headers = {
        "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "tsz-agent-cd",
    }
    client = opener()
    with client.open(urllib.request.Request(endpoint, headers=headers), timeout=15) as response:
        metadata = json.load(response)
    require(
        metadata["id"] == int(artifact_id)
        and metadata["expired"] is False
        and metadata["workflow_run"]["id"] == run_id,
        "artifact_run_mismatch",
    )
    digest = metadata["digest"]
    require(isinstance(digest, str) and digest.startswith("sha256:"), "missing_archive_digest")
    result = {"archive_bytes": metadata["size_in_bytes"], "archive_sha256": digest[7:]}
    check_archive(result, image_bytes)
    try:
        with client.open(urllib.request.Request(f"{endpoint}/zip", headers=headers), timeout=15):
            raise ArtifactError("missing_download_redirect")
    except urllib.error.HTTPError as exc:
        with exc:
            require(exc.code == 302, "cannot_locate_archive")
            result["url"] = check_url(exc.headers.get("Location"))
    return result


def download(stage: Path, image_bytes: int, archive: dict[str, Any]) -> None:
    check_archive(archive, image_bytes)
    url = check_url(archive["url"])
    remaining = archive["archive_bytes"]
    digest = hashlib.sha256()
    with tempfile.TemporaryFile(dir=stage) as output:
        with opener().open(url, timeout=30) as response:
            require(response.status == 200, "archive_download_failed")
            while block := response.read(min(BLOCK, remaining + 1)):
                remaining -= len(block)
                require(remaining >= 0, "archive_size_mismatch")
                digest.update(block)
                output.write(block)
        require(remaining == 0, "archive_size_mismatch")
        require(digest.hexdigest() == archive["archive_sha256"], "archive_digest_mismatch")
        with zipfile.ZipFile(output) as bundle:
            entries = bundle.infolist()
            require(len(entries) == 1, "invalid_archive_members")
            image = entries[0]
            require(
                image.filename == FILENAME
                and image.file_size == image_bytes
                and not stat.S_ISLNK(image.external_attr >> 16),
                "invalid_image_entry",
            )
            with bundle.open(image) as source, (stage / FILENAME).open("xb") as materialized:
                shutil.copyfileobj(source, materialized)


def main(args: list[str]) -> None:
    match args:
        case ["locate", artifact, run, size]:
            print(json.dumps(locate(artifact, int(run), int(size))))
        case ["download", stage, size]:
            download(Path(stage), int(size), json.load(sys.stdin))
            print("Agent image archive downloaded and verified")
        case _:
            raise ArtifactError("invalid_artifact_arguments")


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except ArtifactError as exc:
        print(f"Agent artifact: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"Agent artifact failed: {type(exc).__name__}", file=sys.stderr)
        sys.exit(1)
