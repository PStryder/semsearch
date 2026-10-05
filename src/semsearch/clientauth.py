"""Client-side server authentication: before a client (CLI, tray) sends the admin token, it
checks that the process listening on the configured loopback port IS the SemSearch service.

Why: the token is a bearer secret. Without this check it goes to whatever answers on
127.0.0.1:<port>, which can be another local account's process that bound the port while the
service was stopped or restarting, or a server named by a hostile semsearch.yaml.
"""
from __future__ import annotations

import ipaddress
import socket
import sys
from urllib.parse import urlsplit

LOOPBACK_NAMES = {"127.0.0.1", "localhost", "::1"}


def _listener_pids(port: int) -> set[int]:
    """PIDs of processes listening on TCP <port> (IPv4 loopback or any address)."""
    import ctypes
    from ctypes import wintypes
    AF_INET = 2
    TCP_TABLE_OWNER_PID_LISTENER = 3
    iphlpapi = ctypes.windll.iphlpapi
    size = wintypes.DWORD(0)
    iphlpapi.GetExtendedTcpTable(None, ctypes.byref(size), False, AF_INET, TCP_TABLE_OWNER_PID_LISTENER, 0)
    buf = ctypes.create_string_buffer(size.value + 4096)
    size = wintypes.DWORD(len(buf))
    rc = iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(size), False, AF_INET, TCP_TABLE_OWNER_PID_LISTENER, 0)
    if rc != 0:
        raise OSError(f"GetExtendedTcpTable failed: {rc}")
    n = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0]
    rows = ctypes.cast(ctypes.addressof(buf) + 4, ctypes.POINTER(wintypes.DWORD * 6))
    pids: set[int] = set()
    for i in range(n):
        state, laddr, lport, _raddr, _rport, pid = rows[i]
        if socket.ntohs(lport & 0xFFFF) != port:
            continue
        addr = socket.inet_ntoa(int(laddr).to_bytes(4, "little"))
        if addr in ("127.0.0.1", "0.0.0.0"):
            pids.add(int(pid))
    return pids


def _service_pid(name: str) -> int | None:
    """PID of the running service, 0 if installed but not running, None if not installed."""
    import pywintypes
    import win32service
    try:
        scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
        h = win32service.OpenService(scm, name, win32service.SERVICE_QUERY_STATUS)
    except pywintypes.error as e:
        if e.winerror == 1060:  # ERROR_SERVICE_DOES_NOT_EXIST
            return None
        raise
    st = win32service.QueryServiceStatusEx(h)
    return int(st.get("ProcessId") or 0)


def check_server(base_url: str, service_name: str = "SemSearch") -> tuple[bool, str]:
    """(trusted, reason). Trusted means: a loopback address, and, when the service is installed,
    the listening process is the service's own process."""
    parts = urlsplit(base_url)
    host = (parts.hostname or "").lower()
    if host not in LOOPBACK_NAMES:
        try:
            if not ipaddress.ip_address(host).is_loopback:
                return False, f"{host} is not a loopback address"
        except ValueError:
            return False, f"{host} is not a loopback address"
    if sys.platform != "win32":
        return True, "not Windows: no service identity to check"
    port = parts.port or 80
    try:
        spid = _service_pid(service_name)
    except Exception as e:  # noqa: BLE001
        return False, f"cannot query the {service_name} service: {e}"
    if spid is None:
        return True, "service not installed (development mode)"
    if spid == 0:
        return False, f"the {service_name} service is not running, so whatever listens on port {port} is not it"
    try:
        pids = _listener_pids(port)
    except Exception as e:  # noqa: BLE001
        return False, f"cannot identify the listener on port {port}: {e}"
    if spid in pids:
        return True, f"listener is the {service_name} service (pid {spid})"
    return False, f"port {port} is held by pid(s) {sorted(pids) or 'none'}, not the {service_name} service (pid {spid})"


def token_headers(base_url: str, token: str | None, service_name: str = "SemSearch") -> tuple[dict, str | None]:
    """Headers to send: the token only to a verified server. Returns (headers, warning)."""
    if not token:
        return {}, None
    ok, why = check_server(base_url, service_name)
    if ok:
        return {"x-semsearch-token": token}, None
    return {}, f"admin token withheld: {why}"
