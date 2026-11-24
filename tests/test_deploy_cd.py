"""Exercise the shell CD gates without pointing Docker or SSH at a real deployment."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
SHA = "a" * 40
OLD_ID = "sha256:" + "b" * 64
NEW_ID = "sha256:" + "c" * 64
RUN_ID = 16
RUN_NUMBER = 14


def executable(directory: Path, filename: str, content: str) -> None:
    path = directory / filename
    path.write_text(f"#!{sys.executable}\n{content}")
    path.chmod(0o755)


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, 0),
        ({"main": "e" * 40}, 3),
        ({"latest": 17}, 3),
        ({"run_attempt": 2}, 3),
        ({"head_repository": {"full_name": "fork/tsz-agent"}}, 1),
        ({"event": "pull_request"}, 1),
        ({"status": "in_progress", "conclusion": None}, 1),
        ({"conclusion": "failure"}, 1),
    ],
)
def test_ci_gate_only_accepts_current_successful_main_push(tmp_path, changes, expected):
    data = {
        "main": SHA,
        "latest": RUN_ID,
        "name": "CI",
        "head_sha": SHA,
        "head_repository": {"full_name": "LonelyFellas/tsz-agent"},
        "head_branch": "main",
        "event": "push",
        "run_number": RUN_NUMBER,
        "run_attempt": 1,
        "status": "completed",
        "conclusion": "success",
    } | changes
    executable(
        tmp_path,
        "gh",
        """
import json
import os
import sys
data = json.loads(os.environ["FAKE_CI"])
uri = sys.argv[2]
if uri.endswith("/git/ref/heads/main"):
    print(data["main"])
elif "/actions/workflows/ci.yml/runs?" in uri:
    print(data["latest"])
else:
    print(json.dumps(data))
""",
    )
    outcome = subprocess.run(
        ["bash", DEPLOY / "check-ci.sh", SHA, str(RUN_ID), str(RUN_NUMBER), "1"],
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "FAKE_CI": json.dumps(data)},
        capture_output=True,
        check=False,
    )
    assert outcome.returncode == expected, outcome.stderr.decode()


DOCKER = """
import json
import os
import sys
from pathlib import Path

path = Path(os.environ["FAKE_STATE"])
state = json.loads(path.read_text())
args = sys.argv[1:]
expected = os.environ["NEW_ID"]
previous = os.environ["OLD_ID"]
mode = state["mode"]

def note(event, **updates):
    state["events"].append(event)
    state.update(updates)
    path.write_text(json.dumps(state))

if args[0] == "load":
    note("load")
elif args[:2] == ["image", "inspect"]:
    print(expected, "e" * 40 if mode == "wrong_image" else os.environ["RELEASE_SHA"])
elif args[:2] == ["image", "tag"]:
    note("retain-old")
elif args[0] == "inspect":
    if "{{.State.Status}}" in args[2]:
        print(state["image"], "running", 0)
    else:
        print(state["image"])
elif args[0] == "compose":
    args = args[args.index("-f") + 2:]
    if args[0] == "config":
        pass
    elif args[0] == "run":
        if "schema" in args:
            note("schema")
            if mode == "wrong_database":
                sys.exit(1)
            print(state["current"], state["head"])
        elif "migrate" in args:
            note("migrate")
            if mode == "migration_failed":
                sys.exit(1)
            print(state["head"])
    elif args[0] == "ps":
        if state["running"]:
            print(args[-1] + "-container")
    elif args[0] == "stop":
        note("stop", running=False)
    elif args[0] == "up":
        rollback = os.environ["TSZ_AGENT_IMAGE"].startswith("tsz-agent:rollback-")
        note("rollback" if rollback else "start", running=True, image=previous if rollback else expected)
    elif args[0] == "exec":
        if mode == "unready" and state["image"] != previous:
            sys.exit(1)
else:
    raise AssertionError(args)
"""


@pytest.mark.parametrize(
    ("mode", "current", "expected_status", "expected_events"),
    [
        ("ok", "0009", "accepted", ["load", "schema", "stop", "migrate", "start"]),
        ("wrong_database", "0009", None, ["load", "schema"]),
        ("bad_checksum", "0009", None, []),
        ("wrong_image", "0009", None, ["load"]),
        ("obsolete", "0009", "accepted", []),
        ("migration_failed", "0009", "migration_failed", ["load", "schema", "stop", "migrate"]),
        (
            "unready",
            "0010",
            "rolled_back",
            ["load", "schema", "retain-old", "stop", "start", "rollback"],
        ),
        (
            "unready",
            "0009",
            "needs_recovery",
            ["load", "schema", "stop", "migrate", "start", "stop"],
        ),
    ],
)
def test_publish_preserves_migration_and_rollback_boundaries(
    tmp_path, mode, current, expected_status, expected_events
):
    root = tmp_path / "instance"
    (root / "deploy").mkdir(parents=True)
    (root / "deploy" / "compose.yaml").touch()
    (root / ".env").touch()
    target_state = root / ".agent-cd" / "state"
    if mode == "obsolete":
        target_state.parent.mkdir()
        target_state.write_text("status=accepted\nrun_number=15\nattempt=1\n")

    stage = tmp_path / "stage"
    stage.mkdir()
    script = stage / "publish.sh"
    script.write_bytes((DEPLOY / "publish.sh").read_bytes())
    (stage / "probe.py").touch()
    archive = stage / "image.tar.gz"
    archive.write_bytes(b"fixture image")
    image_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if mode == "bad_checksum":
        image_digest = "e" * 64

    state_path = tmp_path / "docker.json"
    state_path.write_text(
        json.dumps(
            {
                "mode": mode,
                "current": current,
                "head": "0010",
                "image": OLD_ID,
                "running": True,
                "events": [],
            }
        )
    )
    executable(tmp_path, "docker", DOCKER)
    executable(tmp_path, "flock", "pass\n")
    executable(tmp_path, "sleep", "pass\n")

    outcome = subprocess.run(
        [
            "bash",
            script,
            root,
            "agent-test",
            "tsz_agent_test",
            SHA,
            str(RUN_ID),
            str(RUN_NUMBER),
            "1",
            image_digest,
        ],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "NEW_ID": NEW_ID,
            "OLD_ID": OLD_ID,
            "RELEASE_SHA": SHA,
            "FAKE_STATE": str(state_path),
        },
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert (outcome.returncode == 0) == (mode == "ok"), outcome.stderr.decode()
    if expected_status is None:
        assert not target_state.exists()
    else:
        assert f"status={expected_status}\n" in target_state.read_text()
    assert json.loads(state_path.read_text())["events"] == expected_events
