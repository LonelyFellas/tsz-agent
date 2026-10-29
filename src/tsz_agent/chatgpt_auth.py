"""Personal ChatGPT plan OAuth. Credentials belong to this app, never to Codex."""

import asyncio
import base64
import contextlib
import fcntl
import hashlib
import json
import os
import secrets
import stat
import tempfile
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from time import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

import httpx
import jwt
from pydantic import BaseModel, Field, SecretStr, ValidationError

from tsz_agent.clients.tsz import TaskError

ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
AUTHORIZE = ISSUER + "/api/accounts/authorize"
TOKEN = ISSUER + "/api/accounts/oauth/token"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = "openid profile email offline_access resource.invoke " + PLAN_SCOPE
LOCK_TIMEOUT_SECONDS = 5
REVOCATION_TIMEOUT_SECONDS = 5


def auth_directory() -> Path:
    return Path(
        os.environ.get("TSZ_AGENT_CHATGPT_AUTH_DIR", "~/.config/tsz-agent/chatgpt")
    ).expanduser()


def http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False)


class Account(BaseModel):
    client_id: str = Field(min_length=1, max_length=200, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
    session_id: str = Field(default_factory=lambda: str(uuid4()))
    subject: str
    email: str = ""
    scopes: list[str] = Field(default_factory=list)
    access_token: SecretStr | None = None
    refresh_token: SecretStr | None = None
    id_token: SecretStr | None = None
    expires_at: float = 0
    earliest_refresh_at: float = 0

    def public(self) -> dict[str, Any]:
        return {
            "client_id": self.client_id,
            "email": self.email,
            "signed_in": self.access_token is not None,
            "plan_enabled": PLAN_SCOPE in self.scopes and self.access_token is not None,
        }


class Credentials(BaseModel):
    host_id: str = Field(default_factory=lambda: "urn:uuid:" + str(uuid4()))
    active: str | None = None
    accounts: dict[str, Account] = Field(default_factory=dict)


class AuthStore:
    def __init__(self, directory: Path | None = None):
        self.directory = (directory or auth_directory()).expanduser()
        self.path = self.directory / "accounts.json"

    @asynccontextmanager
    async def locked(self) -> AsyncIterator[None]:
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = self.directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                raise TaskError("chatgpt_credentials_unsafe")
            self.directory.chmod(0o700)
            fd = os.open(
                self.directory / "accounts.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
            )
        except OSError as exc:
            raise TaskError("chatgpt_credentials_unavailable") from exc
        try:
            async with asyncio.timeout(LOCK_TIMEOUT_SECONDS):
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        await asyncio.sleep(0.05)
            yield
        except TimeoutError as exc:
            raise TaskError("chatgpt_credentials_busy", retryable=True) from exc
        finally:
            os.close(fd)

    def read(self) -> Credentials:
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return Credentials()
        except OSError as exc:
            raise TaskError("chatgpt_credentials_unsafe") from exc
        try:
            with os.fdopen(fd) as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_mode & 0o077
                    or info.st_size > 1_000_000
                ):
                    raise TaskError("chatgpt_credentials_unsafe")
                return Credentials.model_validate_json(stream.read())
        except (OSError, ValidationError) as exc:
            raise TaskError("chatgpt_credentials_invalid") from exc

    def write(self, value: Credentials) -> None:
        data = value.model_dump(mode="json")
        for key, account in value.accounts.items():
            for field in ("access_token", "refresh_token", "id_token"):
                secret = getattr(account, field)
                data["accounts"][key][field] = secret.get_secret_value() if secret else None
        fd, name = tempfile.mkstemp(prefix=".accounts-", dir=self.directory)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(data, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)

    async def accounts(self) -> Credentials:
        async with self.locked():
            return self.read()

    async def activate(self, client_id: str) -> None:
        async with self.locked():
            data = self.read()
            if client_id not in data.accounts:
                raise TaskError("chatgpt_account_not_found")
            data.active = client_id
            self.write(data)

    async def token(self, client_id: str) -> SecretStr:
        # Lock covers read/refresh/atomic replacement, including across API/worker processes.
        async with self.locked():
            data = self.read()
            account = data.accounts.get(client_id)
            if account is None or account.access_token is None:
                raise TaskError("chatgpt_login_required")
            if PLAN_SCOPE not in account.scopes:
                raise TaskError("chatgpt_plan_not_authorized")
            if account.expires_at <= time() + 60:
                if account.refresh_token is None:
                    raise TaskError("chatgpt_login_required")
                if account.earliest_refresh_at > time():
                    raise TaskError("chatgpt_refresh_not_ready", retryable=True)
                async with http_client() as http:
                    response = await oauth_post(
                        http,
                        TOKEN,
                        {
                            "grant_type": "refresh_token",
                            "client_id": account.client_id,
                            "refresh_token": account.refresh_token.get_secret_value(),
                            "resource": RESOURCE,
                        },
                    )
                if response.status_code != 200:
                    code = error_code(response)
                    if code in {
                        "invalid_grant",
                        "invalid_refresh_token",
                        "token_expired",
                        "refresh_token_expired",
                        "refresh_token_invalidated",
                        "refresh_token_reused",
                    }:
                        clear_tokens(account)
                        self.write(data)
                        raise TaskError("chatgpt_login_required")
                    raise TaskError("chatgpt_refresh_failed", retryable=response.status_code >= 500)
                update_tokens(account, response.json())
                self.write(data)
            if PLAN_SCOPE not in account.scopes:
                raise TaskError("chatgpt_plan_not_authorized")
            assert account.access_token is not None
            return account.access_token

    async def logout(self, client_id: str) -> bool:
        async with self.locked():
            data = self.read()
            account = data.accounts.get(client_id)
            if account is None:
                raise TaskError("chatgpt_account_not_found")
            revoked = account.refresh_token is None
            try:
                if account.refresh_token:
                    async with asyncio.timeout(REVOCATION_TIMEOUT_SECONDS), http_client() as http:
                        discovery = await get_json(
                            http, ISSUER + "/.well-known/openid-configuration"
                        )
                        endpoint = trusted_endpoint(discovery["revocation_endpoint"])
                        for attempt in range(3):
                            try:
                                response = await oauth_post(
                                    http,
                                    endpoint,
                                    {
                                        "token": account.refresh_token.get_secret_value(),
                                        "token_type_hint": "refresh_token",
                                        "client_id": account.client_id,
                                    },
                                )
                                if response.status_code == 200:
                                    revoked = True
                                    break
                                if response.status_code < 500:
                                    break
                            except TaskError as exc:
                                if not exc.retryable:
                                    break
                            if attempt < 2:
                                await asyncio.sleep(0.2 * (attempt + 1))
            except (TaskError, KeyError, TypeError, TimeoutError):
                pass
            clear_tokens(account)
            self.write(data)
            return revoked


def clear_tokens(account: Account) -> None:
    account.access_token = account.refresh_token = account.id_token = None
    account.expires_at = 0


def error_code(response: httpx.Response) -> str | None:
    try:
        error = response.json().get("error")
        return (
            error
            if isinstance(error, str)
            else error.get("code")
            if isinstance(error, dict)
            else None
        )
    except (ValueError, AttributeError):
        return None


def trusted_endpoint(value: Any) -> str:
    if not isinstance(value, str):
        raise TaskError("chatgpt_auth_invalid")
    url = urlsplit(value)
    if url.scheme != "https" or url.netloc != "auth.openai.com" or url.fragment:
        raise TaskError("chatgpt_auth_invalid")
    return value


async def get_json(http: httpx.AsyncClient, url: str) -> Any:
    try:
        response = await http.get(url)
        if response.status_code != 200 or len(response.content) > 1_000_000:
            raise TaskError("chatgpt_auth_unavailable", retryable=True)
        return response.json()
    except (httpx.TransportError, ValueError) as exc:
        raise TaskError("chatgpt_auth_unavailable", retryable=True) from exc


async def oauth_post(http: httpx.AsyncClient, url: str, data: dict[str, str]) -> httpx.Response:
    try:
        response = await http.post(url, data=data)
        if len(response.content) > 1_000_000:
            raise TaskError("chatgpt_auth_invalid")
        return response
    except httpx.TransportError as exc:
        raise TaskError("chatgpt_auth_unavailable", retryable=True) from exc


def update_tokens(account: Account, value: Any) -> None:
    try:
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("access_token"), str)
            or not value["access_token"]
            or str(value.get("token_type", "")).lower() != "bearer"
            or type(value.get("expires_in")) is not int
            or value["expires_in"] <= 0
        ):
            raise ValueError
        account.access_token = SecretStr(value["access_token"])
        account.expires_at = time() + value["expires_in"]
        account.earliest_refresh_at = float(value.get("earliest_refresh_at", 0))
        if "scope" in value:
            account.scopes = value["scope"].split()
        for field in ("refresh_token", "id_token"):
            if field in value:
                if not isinstance(value[field], str) or not value[field]:
                    raise ValueError
                setattr(account, field, SecretStr(value[field]))
    except (ValueError, TypeError, AttributeError) as exc:
        raise TaskError("chatgpt_auth_invalid") from exc


async def verify_identity(
    http: httpx.AsyncClient, token: str, client_id: str, nonce: str
) -> dict[str, Any]:
    try:
        discovery = await get_json(http, ISSUER + "/.well-known/openid-configuration")
        if discovery["issuer"] != ISSUER:
            raise ValueError
        keys = await get_json(http, trusted_endpoint(discovery["jwks_uri"]))
        header = jwt.get_unverified_header(token)
        key = next(k for k in keys["keys"] if k["kid"] == header["kid"])
        claims = jwt.decode(
            token,
            jwt.PyJWK.from_dict(key).key,
            algorithms=["RS256"],
            audience=client_id,
            issuer=ISSUER,
            options={"require": ["iss", "sub", "aud", "exp", "iat", "nonce"]},
        )
        if (
            not isinstance(claims["sub"], str)
            or not claims["sub"]
            or not secrets.compare_digest(claims["nonce"], nonce)
        ):
            raise ValueError
        return claims
    except (jwt.PyJWTError, ValueError, KeyError, TypeError, StopIteration) as exc:
        raise TaskError("chatgpt_identity_invalid") from exc


async def login(
    store: AuthStore, open_browser: Callable[[str], Any], client_id: str | None = None
) -> Account:
    async with store.locked():
        data = store.read()
        store.write(data)  # Persist the host ID before the first authorization attempt.
        previous = data.accounts.get(client_id) if client_id else None
        if client_id and previous is None:
            raise TaskError("chatgpt_account_not_found")
    state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    callback: asyncio.Future[dict[str, str]] = asyncio.get_running_loop().create_future()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        status = "400 Bad Request"
        try:
            async with asyncio.timeout(5):
                request = await reader.readuntil(b"\r\n\r\n")
            method, target, _ = request.split(b"\r\n", 1)[0].decode("ascii").split(" ")
            url = urlsplit(target)
            raw = parse_qs(url.query, max_num_fields=12)
            if (
                method == "GET"
                and url.path == "/auth/callback"
                and all(len(v) == 1 for v in raw.values())
            ):
                values = {k: v[0] for k, v in raw.items()}
                if secrets.compare_digest(values.get("state", ""), state) and not callback.done():
                    callback.set_result(values)
                    status = "200 OK"
        except (
            ValueError,
            UnicodeError,
            TimeoutError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ):
            pass
        finally:
            body = "授权回调已接收，请返回发起登录的页面或终端查看结果。".encode()
            writer.write(
                (
                    f"HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\nCache-Control: no-store\r\nReferrer-Policy: no-referrer\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n"
                ).encode()
                + body
            )
            with contextlib.suppress(ConnectionError):
                await writer.drain()
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, limit=16384)
    redirect = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/auth/callback"
    params = {
        "client_id": previous.client_id if previous else "dynamic_agent_client",
        "ext_agent_host_id": data.host_id,
        "response_type": "code",
        "redirect_uri": redirect,
        "scope": SCOPES,
        "resource": RESOURCE,
        "state": state,
        "nonce": nonce,
        "code_challenge_method": "S256",
        "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("="),
    }
    if previous is None:
        params["agent_name_hint"] = "tsz-agent"
    elif previous.email:
        params["login_hint"] = previous.email
    if previous is not None and PLAN_SCOPE not in previous.scopes:
        # Explicit reauthorization can enable a previously declined plan permission.
        params["prompt"] = "consent"
    # Do not put ID-token hints in URLs that a terminal/browser history could retain.
    try:
        async with server, asyncio.timeout(300):
            open_browser(AUTHORIZE + "?" + urlencode(params))
            result = await callback
        if result.get("error"):
            raise TaskError("chatgpt_login_denied")
        issued = result.get("client_id", previous.client_id if previous else "")
        if (
            not issued
            or issued == "dynamic_agent_client"
            or (previous and issued != previous.client_id)
        ):
            raise TaskError("chatgpt_client_mismatch")
        if not result.get("code"):
            raise TaskError("chatgpt_auth_invalid")
        async with http_client() as http:
            response = await oauth_post(
                http,
                TOKEN,
                {
                    "grant_type": "authorization_code",
                    "client_id": issued,
                    "code": result["code"],
                    "code_verifier": verifier,
                    "redirect_uri": redirect,
                    "resource": RESOURCE,
                },
            )
            if response.status_code != 200:
                raise TaskError("chatgpt_code_exchange_failed")
            value = response.json()
            claims = await verify_identity(http, value["id_token"], issued, nonce)
        if previous and claims["sub"] != previous.subject:
            raise TaskError("chatgpt_account_mismatch")
        account = Account(client_id=issued, subject=claims["sub"], email=claims.get("email", ""))
        update_tokens(account, value)
        async with store.locked():
            data = store.read()
            existing = data.accounts.get(issued)
            if existing and existing.subject != account.subject:
                raise TaskError("chatgpt_account_mismatch")
            data.accounts[issued] = account
            data.active = issued
            store.write(data)
        return account
    except TimeoutError as exc:
        raise TaskError("chatgpt_login_timeout") from exc
    except (ValueError, KeyError, TypeError) as exc:
        raise TaskError("chatgpt_auth_invalid") from exc
