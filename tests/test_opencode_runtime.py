"""Opt-in native OpenCode check; the only provider is a loopback HTTP fixture."""

import asyncio
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from tsz_agent.models.opencode import generate_text, request
from tsz_agent.schemas import ModelSelection


@pytest.mark.skipif(
    not os.getenv("OPENCODE_TEST_BINARY"),
    reason="set OPENCODE_TEST_BINARY for offline native check",
)
async def test_native_direct_transport_preserves_tools_without_executing_them(tmp_path):
    binary = os.environ["OPENCODE_TEST_BINARY"]
    version = subprocess.run([binary, "--version"], capture_output=True, text=True, check=True)
    assert version.stdout.strip() == "opencode v2.1.4"
    captured = []
    execution_marker = tmp_path / "tool-execution-attempted"
    file_marker = tmp_path / "file-must-not-exist"
    shell_marker = tmp_path / "shell-must-not-exist"
    observer_ready = tmp_path / "observer-ready"
    observer = tmp_path / "config" / "opencode" / "plugins" / "observe-tools.js"
    observer.parent.mkdir(parents=True)
    observer.write_text(
        'import { appendFileSync, writeFileSync } from "node:fs";\n'
        'export default { id: "tsz.test.tool-observer", async setup(ctx) {\n'
        'await ctx.tool.hook("execute.before", () => {\n'
        f'appendFileSync({json.dumps(str(execution_marker))}, "attempt\\n");\n'
        "});\n"
        f'writeFileSync({json.dumps(str(observer_ready))}, "ready");\n'
        "} };\n"
    )
    certificate, private_key = tmp_path / "server.crt", tmp_path / "server.key"
    certificate_config = tmp_path / "certificate.cnf"
    certificate_config.write_text(
        "[req]\nprompt=no\ndistinguished_name=dn\nx509_extensions=extensions\n"
        "[dn]\nCN=localhost\n[extensions]\nsubjectAltName=IP:127.0.0.1,DNS:localhost\n"
        "basicConstraints=critical,CA:TRUE\n"
    )
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-config",
            str(certificate_config),
            "-keyout",
            str(private_key),
            "-out",
            str(certificate),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            captured.append(
                {
                    "body": body,
                    "session": self.headers.get("x-opencode-session"),
                    "project": self.headers.get("x-opencode-project"),
                    "client": self.headers.get("x-opencode-client"),
                    "tls": self.connection.version(),
                }
            )
            delta = {"content": "OK"}
            if len(captured) >= 3:
                code = (
                    f"await fs.write({{path: {json.dumps(str(file_marker))}, content: 'unsafe'}})"
                    if len(captured) == 3
                    else f"await shell({{command: {json.dumps('touch ' + str(shell_marker))}}})"
                )
                delta["tool_calls"] = [
                    {
                        "index": 0,
                        "id": "call_fixture",
                        "type": "function",
                        "function": {
                            "name": "execute",
                            "arguments": json.dumps({"code": code}),
                        },
                    }
                ]
            chunk = {
                "id": "fixture",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "text",
                "choices": [
                    {
                        "index": 0,
                        "delta": delta,
                        "finish_reason": "tool_calls" if len(captured) >= 3 else "stop",
                    }
                ],
            }
            payload = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(certificate, private_key)
    provider.socket = tls.wrap_socket(provider.socket, server_side=True)
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    project = tmp_path / "project"
    project.mkdir()
    catalog = tmp_path / "models.json"
    catalog.write_text("{}")
    config = {
        "providers": {
            "fixture": {
                "package": "@opencode/ai/providers/openai-compatible",
                "settings": {
                    "baseURL": f"https://127.0.0.1:{provider.server_port}/v1",
                    "apiKey": "fixture-only",
                    "name": "fixture",
                },
                "models": {
                    "text": {
                        "name": "Fixture",
                        "capabilities": {"tools": True, "input": ["text"], "output": ["text"]},
                    }
                },
            }
        },
    }
    password = "offline-fixture-" + "x" * 32
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "OPENCODE_DB": str(tmp_path / "opencode.db"),
        "OPENCODE_CONFIG_CONTENT": json.dumps(config),
        "OPENCODE_MODELS_PATH": str(catalog),
        "OPENCODE_DISABLE_MODELS_FETCH": "1",
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        "OPENCODE_SERVER_PASSWORD": password,
        "NODE_EXTRA_CA_CERTS": str(certificate),
    }
    command = [binary, "serve", "--hostname", "127.0.0.1", "--port", str(port)]
    if sys.platform == "darwin":
        assert shutil.which("sandbox-exec"), "native offline check requires loopback-only sandbox"
        command = [
            "sandbox-exec",
            "-p",
            '(version 1) (allow default) (deny network-outbound) (allow network-outbound (remote ip "localhost:*"))',
            *command,
        ]
    process = subprocess.Popen(
        command, cwd=project, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            auth=("opencode", password),
            timeout=10,
            trust_env=False,
        ) as http:
            async with asyncio.timeout(25):
                while True:
                    assert process.poll() is None, "isolated OpenCode exited before readiness"
                    try:
                        response = await http.get("/api/model")
                        if response.status_code == 200 and any(
                            m["providerID"] == "fixture" for m in response.json()["data"]
                        ):
                            plugins = (await http.get("/api/plugin")).json()["data"]
                            states = [
                                (p.get("id"), p.get("state", {}).get("status")) for p in plugins
                            ]
                            assert not any(status == "failed" for _, status in states), states
                            if observer_ready.exists():
                                break
                            pytest.fail(f"execution observer not loaded; plugin states: {states}")
                    except httpx.TransportError:
                        pass
                    await asyncio.sleep(0.1)
            choice = ModelSelection(source="opencode", provider_id="fixture", model_id="text")
            assert observer_ready.exists(), "execution observer must be installed before generation"
            baseline = await request(
                http,
                "POST",
                "/api/session",
                {
                    "title": "Native declaration baseline",
                    "agent": "build",
                    "model": {"providerID": "fixture", "id": "text", "variant": "default"},
                },
            )
            try:
                assert (
                    await request(
                        http,
                        "POST",
                        f"/api/session/{baseline['id']}/generate",
                        {"prompt": "Reply with OK"},
                    )
                )["text"] == "OK"
            finally:
                await request(http, "DELETE", f"/api/session/{baseline['id']}", expected_status=204)
            assert await generate_text(http, choice, "Reply with OK") == "OK"
            for prompt in ["Try to write a file", "Try to run a terminal command"]:
                assert await generate_text(http, choice, prompt) == "OK"
            assert len(captured) == 4, (
                "tool calls must never start a tool loop or follow-up request"
            )
            assert not execution_marker.exists(), "no tool execution may even be attempted"
            assert not file_marker.exists() and not shell_marker.exists()
            normal_tools = captured[0]["body"]["tools"]
            assert any(t["function"]["name"] == "execute" for t in normal_tools)
            for item in captured:
                assert item["session"] and item["project"] and item["client"] == "cli"
                assert item["tls"].startswith("TLSv"), (
                    "OpenCode must connect directly to the HTTPS fixture"
                )
                assert item["body"]["tools"] == normal_tools
                assert any(m["role"] == "system" for m in item["body"]["messages"])
            assert len({item["session"] for item in captured}) == 4
            response = await http.get("/api/permission/request")
            assert response.status_code == 200 and response.json()["data"] == []
            response = await http.get("/api/session")
            assert response.status_code == 200 and response.json()["data"] == []
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        provider.shutdown()
        provider.server_close()
        thread.join(timeout=5)
        shutil.rmtree(tmp_path)
