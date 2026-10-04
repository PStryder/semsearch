"""Operator-side root management shared by the CLI and the tray: grant the service account
read access on a folder (an owner can do that without elevation), then tell the running
service to add/remove the root through the token-gated API. Nothing here runs inside the
service: the service cannot grant itself rights, by design.
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass

SERVICE_ACCOUNT = r"NT SERVICE\SemSearch"


@dataclass(slots=True)
class GrantResult:
    path: str
    ok: bool
    already: bool = False
    detail: str = ""


def _icacls(args: list[str]) -> tuple[int, str]:
    p = subprocess.run(["icacls", *args], capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return p.returncode, (p.stdout + p.stderr).strip()


def has_read_grant(path: str, account: str = SERVICE_ACCOUNT) -> bool:
    rc, out = _icacls([path])
    return rc == 0 and (account.lower() + ":") in out.lower()


def grant_service_read(path: str, account: str = SERVICE_ACCOUNT) -> GrantResult:
    """One inheritable read/execute ACE on the folder (no /T: the kernel propagates it)."""
    if sys.platform != "win32":
        return GrantResult(path, False, detail="Windows only")
    if has_read_grant(path, account):
        return GrantResult(path, True, already=True)
    rc, out = _icacls([path, "/grant", f"{account}:(OI)(CI)RX", "/Q"])
    if rc != 0:
        hint = " (you are not the owner of this folder: run `semsearch roots add` from an elevated prompt)" if "denied" in out.lower() else ""
        return GrantResult(path, False, detail=(out or f"icacls exit {rc}") + hint)
    return GrantResult(path, True)


def revoke_service_read(path: str, account: str = SERVICE_ACCOUNT) -> GrantResult:
    if sys.platform != "win32":
        return GrantResult(path, False, detail="Windows only")
    if not has_read_grant(path, account):
        return GrantResult(path, True, already=True)
    rc, out = _icacls([path, "/remove:g", account, "/Q"])
    return GrantResult(path, rc == 0, detail=out if rc else "")


def service_account_from_config(cfg) -> str:
    """The ACL spelling of the account the service runs as (from the install manifest when
    present; the default virtual account otherwise)."""
    try:
        import json
        inst = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "SemSearch", "install-manifest.json")
        with open(inst, encoding="utf-8-sig") as f:
            m = json.load(f)
        return m.get("acl_account") or SERVICE_ACCOUNT
    except (OSError, ValueError):
        return SERVICE_ACCOUNT


def add_root(client, path: str, token: str, account: str = SERVICE_ACCOUNT, grant: bool = True) -> dict:
    """Grant, then add through the API. `client` is an httpx.Client bound to the service."""
    p = os.path.abspath(path)
    if not os.path.isdir(p):
        raise ValueError(f"not a directory: {p}")
    g = grant_service_read(p, account) if grant else GrantResult(p, True, already=True)
    if not g.ok:
        raise PermissionError(f"could not grant {account} read access on {p}: {g.detail}")
    r = client.post("/config/roots", json={"add": p}, headers={"x-semsearch-token": token})
    if r.status_code >= 400:
        raise RuntimeError(f"service refused the root: {r.json().get('detail', r.text)}")
    out = r.json()
    out["grant"] = "already present" if g.already else "granted"
    return out


def remove_root(client, path: str, token: str, account: str = SERVICE_ACCOUNT, revoke: bool = True) -> dict:
    p = os.path.abspath(path)
    r = client.post("/config/roots", json={"remove": p}, headers={"x-semsearch-token": token})
    if r.status_code >= 400:
        raise RuntimeError(f"service refused: {r.json().get('detail', r.text)}")
    out = r.json()
    if revoke and os.path.isdir(p):
        g = revoke_service_read(p, account)
        out["grant"] = "revoked" if (g.ok and not g.already) else ("was not present" if g.already else f"not revoked: {g.detail}")
    return out
