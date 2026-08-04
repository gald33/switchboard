"""HTTP client for a Switchboard hub.

Depends only on ``httpx`` so an agent can talk to a hub without installing the
server. Both a sync and an async client are provided; the CLI uses the sync
one, the MCP bridge uses the async one.
"""

from __future__ import annotations

import os
import platform
import socket
import subprocess
import uuid
from dataclasses import dataclass
from typing import Any, Sequence

import httpx

from .config import ClientConfig

__all__ = [
    "SwitchboardError",
    "LeaseHeld",
    "Client",
    "AsyncClient",
    "Identity",
    "detect_identity",
]


class SwitchboardError(RuntimeError):
    """A hub returned an error response."""

    def __init__(self, message: str, *, status: int | None = None,
                 payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.payload = payload or {}


class LeaseHeld(SwitchboardError):
    """Someone else holds the lease you asked for."""

    @property
    def holder(self) -> str | None:
        return self.payload.get("holder")

    @property
    def expires_in(self) -> float | None:
        return self.payload.get("expires_in")


# --- identity ---------------------------------------------------------------


@dataclass
class Identity:
    """Who this agent is, inferred from the environment it is running in."""

    agent_id: str
    name: str
    kind: str
    branch: str | None
    meta: dict[str, Any]


def _git(*args: str, cwd: str | None = None) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = out.stdout.strip()
    return value or None


def detect_identity(
    *,
    agent_id: str | None = None,
    name: str | None = None,
    kind: str | None = None,
    cwd: str | None = None,
) -> Identity:
    """Infer a stable-ish identity for the current session.

    The agent id is stable across restarts on the same machine + branch, so a
    resumed session reclaims its own leases instead of colliding with itself.
    """
    branch = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd)
    repo = _git("rev-parse", "--show-toplevel", cwd=cwd)
    repo_name = os.path.basename(repo) if repo else os.path.basename(os.getcwd())
    host = socket.gethostname()

    if kind is None:
        if os.environ.get("GITHUB_ACTIONS"):
            kind = "ci"
        elif os.environ.get("CLAUDE_CODE_REMOTE") or os.environ.get("CODESPACES"):
            kind = "cloud"
        else:
            kind = "local"

    if agent_id is None:
        agent_id = os.environ.get("SWITCHBOARD_AGENT_ID")
    if agent_id is None:
        slug = (branch or "detached").replace("/", "-")
        agent_id = f"{kind}-{slug}-{host}"[:96]

    if name is None:
        name = os.environ.get("SWITCHBOARD_AGENT_NAME") or f"{repo_name}:{branch or 'detached'}"

    meta = {
        "host": host,
        "repo": repo_name,
        "platform": platform.system(),
        "pid": os.getpid(),
    }
    if os.environ.get("GITHUB_RUN_ID"):
        meta["github_run_id"] = os.environ["GITHUB_RUN_ID"]
    return Identity(agent_id=agent_id, name=name, kind=kind, branch=branch, meta=meta)


def _raise_for(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    try:
        payload = response.json()
    except ValueError:
        payload = {"detail": response.text}
    detail = payload.get("detail") or payload.get("error") or response.text
    if payload.get("error") == "lease_conflict":
        raise LeaseHeld(str(detail), status=response.status_code, payload=payload)
    raise SwitchboardError(str(detail), status=response.status_code, payload=payload)


def _headers(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


class _Base:
    def __init__(self, config: ClientConfig | None = None, *, agent_id: str | None = None) -> None:
        self.config = config or ClientConfig.from_env()
        self.agent_id = agent_id or self.config.agent_id or f"agent-{uuid.uuid4().hex[:12]}"
        self.workspace = self.config.workspace

    def _ws(self, workspace: str | None) -> str:
        return workspace or self.workspace


class Client(_Base):
    """Synchronous client."""

    def __init__(self, config: ClientConfig | None = None, *, agent_id: str | None = None,
                 timeout: float = 40.0) -> None:
        super().__init__(config, agent_id=agent_id)
        self._http = httpx.Client(
            base_url=self.config.url, headers=_headers(self.config.token), timeout=timeout
        )

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self._http.request(method, path, **kwargs)
        _raise_for(response)
        return response.json()

    # --- meta ---
    def health(self) -> dict[str, Any]:
        return self._call("GET", "/health")

    def stats(self, workspace: str | None = None) -> dict[str, Any]:
        return self._call("GET", "/stats", params={"workspace": self._ws(workspace)})

    # --- presence ---
    def register(self, *, name: str, kind: str = "unknown", branch: str | None = None,
                 task: str | None = None, channels: Sequence[str] = (),
                 meta: dict[str, Any] | None = None, ttl: float | None = None,
                 workspace: str | None = None) -> dict[str, Any]:
        return self._call("POST", "/agents/register", json={
            "workspace": self._ws(workspace), "agent_id": self.agent_id, "name": name,
            "kind": kind, "branch": branch, "task": task, "channels": list(channels),
            "meta": meta or {}, "ttl": ttl,
        })["agent"]

    def heartbeat(self, *, task: str | None = None, ttl: float | None = None,
                  renew_leases: bool = True, workspace: str | None = None) -> dict[str, Any]:
        return self._call("POST", "/agents/heartbeat", json={
            "workspace": self._ws(workspace), "agent_id": self.agent_id, "task": task,
            "ttl": ttl, "renew_leases": renew_leases,
        })

    def agents(self, workspace: str | None = None) -> list[dict[str, Any]]:
        return self._call("GET", "/agents", params={"workspace": self._ws(workspace)})["agents"]

    def deregister(self, workspace: str | None = None) -> bool:
        return self._call("DELETE", f"/agents/{self.agent_id}",
                          params={"workspace": self._ws(workspace)})["removed"]

    # --- leases ---
    def acquire(self, resource: str, *, note: str | None = None, ttl: float | None = None,
                workspace: str | None = None) -> dict[str, Any]:
        return self._call("POST", "/leases/acquire", json={
            "workspace": self._ws(workspace), "resource": resource,
            "agent_id": self.agent_id, "note": note, "ttl": ttl,
        })["lease"]

    def renew(self, resource: str, *, ttl: float | None = None,
              workspace: str | None = None) -> dict[str, Any]:
        return self._call("POST", "/leases/renew", json={
            "workspace": self._ws(workspace), "resource": resource,
            "agent_id": self.agent_id, "ttl": ttl,
        })["lease"]

    def release(self, resource: str, *, force: bool = False,
                workspace: str | None = None) -> bool:
        return self._call("POST", "/leases/release", json={
            "workspace": self._ws(workspace), "resource": resource,
            "agent_id": self.agent_id, "force": force,
        })["released"]

    def leases(self, *, holder: str | None = None,
               workspace: str | None = None) -> list[dict[str, Any]]:
        params = {"workspace": self._ws(workspace)}
        if holder:
            params["holder"] = holder
        return self._call("GET", "/leases", params=params)["leases"]

    # --- messages ---
    def post(self, channel: str, body: Any, *, type: str = "note", thread: str | None = None,
             ttl: float | None = None, workspace: str | None = None) -> dict[str, Any]:
        return self._call("POST", "/messages", json={
            "workspace": self._ws(workspace), "channel": channel, "agent_id": self.agent_id,
            "body": body, "type": type, "thread": thread, "ttl": ttl,
        })["message"]

    def send(self, to_agent: str, body: Any, **kwargs: Any) -> dict[str, Any]:
        """Direct message — sugar for posting to the recipient's ``@`` channel."""
        return self.post(f"@{to_agent}", body, **kwargs)

    def inbox(self, *, channels: Sequence[str] | None = None, wait: float = 0.0,
              limit: int = 100, peek: bool = False, include_own: bool = False,
              workspace: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "workspace": self._ws(workspace), "agent_id": self.agent_id,
            "wait": wait, "limit": limit, "peek": peek, "include_own": include_own,
        }
        if channels:
            params["channel"] = list(channels)
        return self._call("GET", "/inbox", params=params)["messages"]

    def history(self, channel: str, *, limit: int = 50,
                workspace: str | None = None) -> list[dict[str, Any]]:
        return self._call("GET", f"/channels/{channel}",
                          params={"workspace": self._ws(workspace),
                                  "limit": limit})["messages"]

    def channels(self, workspace: str | None = None) -> list[dict[str, Any]]:
        return self._call("GET", "/channels",
                          params={"workspace": self._ws(workspace)})["channels"]

    # --- blackboard ---
    def board_set(self, key: str, value: Any, *, ttl: float | None = None,
                  if_revision: int | None = None,
                  workspace: str | None = None) -> dict[str, Any]:
        return self._call("PUT", "/board", json={
            "workspace": self._ws(workspace), "key": key, "value": value,
            "agent_id": self.agent_id, "ttl": ttl, "if_revision": if_revision,
        })["entry"]

    def board_get(self, key: str, *, default: Any = None,
                  workspace: str | None = None) -> Any:
        try:
            entry = self._call("GET", f"/board/{key}",
                               params={"workspace": self._ws(workspace)})["entry"]
        except SwitchboardError as exc:
            if exc.status == 404:
                return default
            raise
        return entry["value"]

    def board_entry(self, key: str, *, workspace: str | None = None) -> dict[str, Any] | None:
        try:
            return self._call("GET", f"/board/{key}",
                              params={"workspace": self._ws(workspace)})["entry"]
        except SwitchboardError as exc:
            if exc.status == 404:
                return None
            raise

    def board_list(self, *, prefix: str | None = None,
                   workspace: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"workspace": self._ws(workspace)}
        if prefix:
            params["prefix"] = prefix
        return self._call("GET", "/board", params=params)["entries"]

    def board_delete(self, key: str, *, workspace: str | None = None) -> bool:
        return self._call("DELETE", f"/board/{key}",
                          params={"workspace": self._ws(workspace)})["deleted"]


class AsyncClient(_Base):
    """Asynchronous client — same surface as :class:`Client`."""

    def __init__(self, config: ClientConfig | None = None, *, agent_id: str | None = None,
                 timeout: float = 40.0) -> None:
        super().__init__(config, agent_id=agent_id)
        self._http = httpx.AsyncClient(
            base_url=self.config.url, headers=_headers(self.config.token), timeout=timeout
        )

    async def __aenter__(self) -> AsyncClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = await self._http.request(method, path, **kwargs)
        _raise_for(response)
        return response.json()

    async def health(self) -> dict[str, Any]:
        return await self._call("GET", "/health")

    async def stats(self, workspace: str | None = None) -> dict[str, Any]:
        return await self._call("GET", "/stats", params={"workspace": self._ws(workspace)})

    async def register(self, *, name: str, kind: str = "unknown", branch: str | None = None,
                       task: str | None = None, channels: Sequence[str] = (),
                       meta: dict[str, Any] | None = None, ttl: float | None = None,
                       workspace: str | None = None) -> dict[str, Any]:
        result = await self._call("POST", "/agents/register", json={
            "workspace": self._ws(workspace), "agent_id": self.agent_id, "name": name,
            "kind": kind, "branch": branch, "task": task, "channels": list(channels),
            "meta": meta or {}, "ttl": ttl,
        })
        return result["agent"]

    async def heartbeat(self, *, task: str | None = None, ttl: float | None = None,
                        renew_leases: bool = True,
                        workspace: str | None = None) -> dict[str, Any]:
        return await self._call("POST", "/agents/heartbeat", json={
            "workspace": self._ws(workspace), "agent_id": self.agent_id, "task": task,
            "ttl": ttl, "renew_leases": renew_leases,
        })

    async def agents(self, workspace: str | None = None) -> list[dict[str, Any]]:
        result = await self._call("GET", "/agents", params={"workspace": self._ws(workspace)})
        return result["agents"]

    async def deregister(self, workspace: str | None = None) -> bool:
        result = await self._call("DELETE", f"/agents/{self.agent_id}",
                                  params={"workspace": self._ws(workspace)})
        return result["removed"]

    async def acquire(self, resource: str, *, note: str | None = None, ttl: float | None = None,
                      workspace: str | None = None) -> dict[str, Any]:
        result = await self._call("POST", "/leases/acquire", json={
            "workspace": self._ws(workspace), "resource": resource,
            "agent_id": self.agent_id, "note": note, "ttl": ttl,
        })
        return result["lease"]

    async def renew(self, resource: str, *, ttl: float | None = None,
                    workspace: str | None = None) -> dict[str, Any]:
        result = await self._call("POST", "/leases/renew", json={
            "workspace": self._ws(workspace), "resource": resource,
            "agent_id": self.agent_id, "ttl": ttl,
        })
        return result["lease"]

    async def release(self, resource: str, *, force: bool = False,
                      workspace: str | None = None) -> bool:
        result = await self._call("POST", "/leases/release", json={
            "workspace": self._ws(workspace), "resource": resource,
            "agent_id": self.agent_id, "force": force,
        })
        return result["released"]

    async def leases(self, *, holder: str | None = None,
                     workspace: str | None = None) -> list[dict[str, Any]]:
        params = {"workspace": self._ws(workspace)}
        if holder:
            params["holder"] = holder
        result = await self._call("GET", "/leases", params=params)
        return result["leases"]

    async def post(self, channel: str, body: Any, *, type: str = "note",
                   thread: str | None = None, ttl: float | None = None,
                   workspace: str | None = None) -> dict[str, Any]:
        result = await self._call("POST", "/messages", json={
            "workspace": self._ws(workspace), "channel": channel, "agent_id": self.agent_id,
            "body": body, "type": type, "thread": thread, "ttl": ttl,
        })
        return result["message"]

    async def send(self, to_agent: str, body: Any, **kwargs: Any) -> dict[str, Any]:
        return await self.post(f"@{to_agent}", body, **kwargs)

    async def inbox(self, *, channels: Sequence[str] | None = None, wait: float = 0.0,
                    limit: int = 100, peek: bool = False, include_own: bool = False,
                    workspace: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "workspace": self._ws(workspace), "agent_id": self.agent_id,
            "wait": wait, "limit": limit, "peek": peek, "include_own": include_own,
        }
        if channels:
            params["channel"] = list(channels)
        result = await self._call("GET", "/inbox", params=params)
        return result["messages"]

    async def history(self, channel: str, *, limit: int = 50,
                      workspace: str | None = None) -> list[dict[str, Any]]:
        result = await self._call("GET", f"/channels/{channel}",
                                  params={"workspace": self._ws(workspace), "limit": limit})
        return result["messages"]

    async def channels(self, workspace: str | None = None) -> list[dict[str, Any]]:
        result = await self._call("GET", "/channels",
                                  params={"workspace": self._ws(workspace)})
        return result["channels"]

    async def board_set(self, key: str, value: Any, *, ttl: float | None = None,
                        if_revision: int | None = None,
                        workspace: str | None = None) -> dict[str, Any]:
        result = await self._call("PUT", "/board", json={
            "workspace": self._ws(workspace), "key": key, "value": value,
            "agent_id": self.agent_id, "ttl": ttl, "if_revision": if_revision,
        })
        return result["entry"]

    async def board_get(self, key: str, *, default: Any = None,
                        workspace: str | None = None) -> Any:
        try:
            result = await self._call("GET", f"/board/{key}",
                                      params={"workspace": self._ws(workspace)})
        except SwitchboardError as exc:
            if exc.status == 404:
                return default
            raise
        return result["entry"]["value"]

    async def board_list(self, *, prefix: str | None = None,
                         workspace: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"workspace": self._ws(workspace)}
        if prefix:
            params["prefix"] = prefix
        result = await self._call("GET", "/board", params=params)
        return result["entries"]

    async def board_delete(self, key: str, *, workspace: str | None = None) -> bool:
        result = await self._call("DELETE", f"/board/{key}",
                                  params={"workspace": self._ws(workspace)})
        return result["deleted"]
