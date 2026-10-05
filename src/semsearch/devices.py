"""Accelerator discovery and stable device resolution.

DirectML's ``device_id`` is the adapter's position in the DXGI enumeration, which can change
across reboots, driver updates and hardware changes. Configuration therefore names devices by
stable identity (vendor id + device id, optionally name substring / integrated flag), and this
module resolves those selectors to the current ordinal at startup, logging what it chose.

Enumeration is done with DXGI (``CreateDXGIFactory1`` + ``EnumAdapters1`` + ``GetDesc1``)
through ctypes, so it reflects the same order ONNX Runtime's DirectML provider sees. Software
adapters (Microsoft Basic Render Driver) are reported but never selected.
"""
from __future__ import annotations

import ctypes
import logging
from ctypes import POINTER, Structure, byref, c_size_t, c_uint, c_void_p, c_wchar
from dataclasses import asdict, dataclass
from typing import Any

log = logging.getLogger(__name__)

VENDORS = {0x1002: "amd", 0x10DE: "nvidia", 0x8086: "intel", 0x1414: "microsoft", 0x5143: "qualcomm"}
DXGI_ADAPTER_FLAG_SOFTWARE = 2


class _LUID(Structure):
    _fields_ = [("LowPart", ctypes.c_uint32), ("HighPart", ctypes.c_int32)]


class _DXGI_ADAPTER_DESC1(Structure):
    _fields_ = [
        ("Description", c_wchar * 128), ("VendorId", c_uint), ("DeviceId", c_uint), ("SubSysId", c_uint), ("Revision", c_uint),
        ("DedicatedVideoMemory", c_size_t), ("DedicatedSystemMemory", c_size_t), ("SharedSystemMemory", c_size_t),
        ("AdapterLuid", _LUID), ("Flags", c_uint),
    ]


class _GUID(Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16), ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]


def _guid(s: str) -> _GUID:
    import uuid
    u = uuid.UUID(s)
    g = _GUID()
    g.Data1, g.Data2, g.Data3 = u.time_low, u.time_mid, u.time_hi_version
    for i, b in enumerate(u.bytes[8:]):
        g.Data4[i] = b
    return g


@dataclass(slots=True)
class Adapter:
    ordinal: int               # DXGI enumeration index == DirectML device_id
    name: str
    vendor_id: int
    device_id: int
    subsys_id: int
    revision: int
    dedicated_vram_mb: int
    shared_memory_mb: int
    luid: str                  # per-boot identifier (not stable across reboots)
    software: bool
    integrated: bool           # heuristic: no dedicated VRAM worth mentioning
    address: str | None = None  # PCI bus.device.function from the kernel display driver (stable; unique per slot)

    @property
    def vendor(self) -> str:
        return VENDORS.get(self.vendor_id, f"0x{self.vendor_id:04x}")

    @property
    def stable_id(self) -> str:
        """vendor:device:subsys plus the PCI address when known: unique even for two identical cards."""
        base = f"{self.vendor_id:04x}:{self.device_id:04x}:{self.subsys_id:08x}"
        return f"{base}@{self.address}" if self.address else base

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["vendor"] = self.vendor
        d["stable_id"] = self.stable_id
        return d


# ---- kernel display driver: LUID -> PCI address (gdi32 D3DKMT*) ----
class _D3DKMT_OPENADAPTERFROMLUID(Structure):
    _fields_ = [("AdapterLuid", _LUID), ("hAdapter", c_uint)]


class _D3DKMT_QUERYADAPTERINFO(Structure):
    _fields_ = [("hAdapter", c_uint), ("Type", c_uint), ("pPrivateDriverData", c_void_p), ("PrivateDriverDataSize", c_uint)]


class _D3DKMT_ADAPTERADDRESS(Structure):
    _fields_ = [("BusNumber", c_uint), ("DeviceNumber", c_uint), ("FunctionNumber", c_uint)]


class _D3DKMT_CLOSEADAPTER(Structure):
    _fields_ = [("hAdapter", c_uint)]


KMTQAITYPE_ADAPTERADDRESS = 6  # d3dkmthk.h: UMDRIVERPRIVATE=0, UMDRIVERNAME, UMOPENGLINFO, GETSEGMENTSIZE, ADAPTERGUID, FLIPQUEUEINFO, ADAPTERADDRESS=6


def pci_address_for_luid(high: int, low: int) -> str | None:
    """'pci:<bus>.<device>.<function>' for a DXGI adapter LUID, or None (software adapters,
    non-PCI devices, or when the kernel interface refuses)."""
    try:
        gdi = ctypes.windll.gdi32
        oa = _D3DKMT_OPENADAPTERFROMLUID()
        oa.AdapterLuid.HighPart, oa.AdapterLuid.LowPart = high, low
        if gdi.D3DKMTOpenAdapterFromLuid(byref(oa)) != 0:
            return None
        try:
            addr = _D3DKMT_ADAPTERADDRESS()
            q = _D3DKMT_QUERYADAPTERINFO(oa.hAdapter, KMTQAITYPE_ADAPTERADDRESS, ctypes.cast(byref(addr), c_void_p), ctypes.sizeof(addr))
            if gdi.D3DKMTQueryAdapterInfo(byref(q)) != 0:
                return None
            return f"pci:{addr.BusNumber}.{addr.DeviceNumber}.{addr.FunctionNumber}"
        finally:
            gdi.D3DKMTCloseAdapter(byref(_D3DKMT_CLOSEADAPTER(oa.hAdapter)))
    except Exception:  # noqa: BLE001
        return None


def enumerate_adapters() -> list[Adapter]:
    """All DXGI adapters in enumeration order. Returns [] when DXGI is unavailable."""
    try:
        dxgi = ctypes.windll.dxgi
    except (AttributeError, OSError):
        return []
    factory = c_void_p()
    iid = _guid("770aae78-f26f-4dba-a829-253c83d1b387")  # IDXGIFactory1
    hr = dxgi.CreateDXGIFactory1(byref(iid), byref(factory))
    if hr != 0 or not factory:
        log.warning("CreateDXGIFactory1 failed: 0x%08x", hr & 0xFFFFFFFF)
        return []
    # vtable: IUnknown(3) + IDXGIObject(4) + IDXGIFactory(5: EnumAdapters, MakeWindowAssociation, GetWindowAssociation,
    # CreateSwapChain, CreateSoftwareAdapter) + IDXGIFactory1(EnumAdapters1=12, IsCurrent=13)
    vtbl = ctypes.cast(factory, POINTER(POINTER(c_void_p)))[0]
    EnumAdapters1 = ctypes.WINFUNCTYPE(ctypes.c_long, c_void_p, c_uint, POINTER(c_void_p))(vtbl[12])
    Release = ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)(vtbl[2])
    out: list[Adapter] = []
    i = 0
    while True:
        adapter = c_void_p()
        hr = EnumAdapters1(factory, i, byref(adapter))
        if hr != 0 or not adapter:
            break
        avt = ctypes.cast(adapter, POINTER(POINTER(c_void_p)))[0]
        # IDXGIAdapter1: IUnknown(3) + IDXGIObject(4) + IDXGIAdapter(EnumOutputs=7, GetDesc=8, CheckInterfaceSupport=9) + GetDesc1=10
        GetDesc1 = ctypes.WINFUNCTYPE(ctypes.c_long, c_void_p, POINTER(_DXGI_ADAPTER_DESC1))(avt[10])
        ARelease = ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)(avt[2])
        desc = _DXGI_ADAPTER_DESC1()
        if GetDesc1(adapter, byref(desc)) == 0:
            dedicated = int(desc.DedicatedVideoMemory) // (1024 * 1024)
            software = bool(desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE)
            out.append(Adapter(
                ordinal=i, name=desc.Description, vendor_id=desc.VendorId, device_id=desc.DeviceId, subsys_id=desc.SubSysId,
                revision=desc.Revision, dedicated_vram_mb=dedicated, shared_memory_mb=int(desc.SharedSystemMemory) // (1024 * 1024),
                luid=f"{desc.AdapterLuid.HighPart:08x}-{desc.AdapterLuid.LowPart:08x}",
                software=software, integrated=dedicated < 1024,
                address=None if software else pci_address_for_luid(desc.AdapterLuid.HighPart, desc.AdapterLuid.LowPart)))
        ARelease(adapter)
        i += 1
    Release(factory)
    return out


def _parse_hex(v) -> int | None:
    if v is None:
        return None
    if isinstance(v, int):
        return v
    s = str(v).strip().lower()
    return int(s, 16) if s.startswith("0x") else int(s, 16) if all(c in "0123456789abcdef" for c in s) else int(s)


def match_adapter(selector: dict[str, Any], adapters: list[Adapter]) -> Adapter | None:
    """Pick the adapter matching a selector: {vendor: amd|nvidia|intel|0x1002, device: 0x164e,
    name: substring, integrated: bool, ordinal: n (last resort)}. Software adapters never match.
    When several match, the first in DXGI order wins and a warning is logged."""
    cands = [a for a in adapters if not a.software]
    v = selector.get("vendor")
    if v is not None:
        vid = next((k for k, name in VENDORS.items() if name == str(v).lower()), None) if not str(v).lower().startswith("0x") and not str(v).isdigit() else _parse_hex(v)
        if vid is None:
            try:
                vid = _parse_hex(v)
            except ValueError:
                vid = -1
        cands = [a for a in cands if a.vendor_id == vid]
    if selector.get("device") is not None:
        did = _parse_hex(selector["device"])
        cands = [a for a in cands if a.device_id == did]
    if selector.get("name"):
        needle = str(selector["name"]).lower()
        cands = [a for a in cands if needle in a.name.lower()]
    if selector.get("integrated") is not None:
        cands = [a for a in cands if a.integrated == bool(selector["integrated"])]
    if selector.get("address"):
        cands = [a for a in cands if a.address == str(selector["address"]).lower()]
    if selector.get("subsys") is not None:
        sid = _parse_hex(selector["subsys"])
        cands = [a for a in cands if a.subsys_id == sid]
    if selector.get("ordinal") is not None and not any(k in selector for k in ("vendor", "device", "name", "integrated")):
        cands = [a for a in cands if a.ordinal == int(selector["ordinal"])]
    if not cands:
        return None
    if len(cands) > 1:
        log.warning("selector %s matches %d adapters (%s); using the first", selector, len(cands), ", ".join(f"{a.ordinal}:{a.name}" for a in cands))
    return cands[0]


def resolve_role(spec: str, named: dict[str, dict[str, Any]], adapters: list[Adapter], fallback: str = "cpu") -> tuple[str, str]:
    """Turn a role setting into a concrete runtime device string.

    spec may be: 'cpu', 'cuda[:n]', 'dml:<ordinal>' (legacy, unstable), 'auto', or a logical
    name defined in `named` (e.g. 'integrated-radeon' -> {vendor: amd, integrated: true}).
    Returns (device_string, explanation)."""
    s = (spec or "cpu").strip()
    low = s.lower()
    if low in ("cpu", "auto") or low.startswith("cuda") or low.startswith("dml:"):
        if low.startswith("dml:"):
            n = int(low.split(":", 1)[1] or 0)
            a = next((x for x in adapters if x.ordinal == n), None)
            return s, f"dml ordinal {n} = {a.name if a else 'unknown adapter'} (ordinal selectors are not stable across reboots; prefer a named selector)"
        return s, "as configured"
    sel = named.get(s)
    if sel is None:
        raise ValueError(f"device '{s}' is neither a runtime device (cpu/cuda/dml:n/auto) nor a name defined under embedding.devices")
    a = match_adapter(sel, adapters)
    if a is None:
        return fallback, f"'{s}' {sel} matched no present adapter; fell back to {fallback}"
    return f"dml:{a.ordinal}", f"'{s}' -> {a.name} (vendor {a.vendor}, device 0x{a.device_id:04x}, {a.address or 'no pci address'}, ordinal {a.ordinal}, luid {a.luid})"


def selector_for(a: Adapter) -> dict[str, Any]:
    """The strongest selector we can write for an adapter: vendor + device + subsystem, plus the
    PCI address when the kernel reports one, so two identical cards never collide."""
    sel: dict[str, Any] = {"vendor": f"0x{a.vendor_id:04x}", "device": f"0x{a.device_id:04x}", "subsys": f"0x{a.subsys_id:08x}"}
    if a.address:
        sel["address"] = a.address
    return sel


# ---- hardware profiles: the one choice offered to the user (tray / API) ----
# "light": everyday indexing on the integrated GPU (the CPU when there is none); the dedicated
#          GPU only takes the big jobs (a full build, a deep queue) as the bulk device.
# "gpu":   all document embedding on the dedicated GPU.
# Search queries stay on the CPU in both (fastest for one short text; measured 2.9 ms).
PROFILES = ("light", "gpu")


def named_present(named: dict[str, dict[str, Any]], adapters: list[Adapter]) -> dict[str, str | None]:
    """The first logical name under `embedding.devices` that matches a present integrated and a
    present dedicated adapter ({'integrated': name|None, 'dedicated': name|None})."""
    out: dict[str, str | None] = {"integrated": None, "dedicated": None}
    for name, sel in (named or {}).items():
        a = match_adapter(sel or {}, adapters)
        if a is None:
            continue
        kind = "integrated" if a.integrated else "dedicated"
        if out[kind] is None:
            out[kind] = name
    return out


def profile_specs(profile: str, named: dict[str, dict[str, Any]], adapters: list[Adapter]) -> dict[str, str]:
    """The `device` / `bulk_device` settings a profile stands for on this machine. Raises
    ValueError for an unknown profile, or 'gpu' without a dedicated GPU."""
    if profile not in PROFILES:
        raise ValueError(f"unknown hardware profile {profile!r}; use one of {', '.join(PROFILES)}")
    have = named_present(named, adapters)
    if profile == "gpu":
        if not have["dedicated"]:
            raise ValueError("no dedicated GPU is defined under embedding.devices and present on this machine")
        return {"device": have["dedicated"], "bulk_device": have["dedicated"]}
    steady = have["integrated"] or "cpu"
    return {"device": steady, "bulk_device": have["dedicated"] or steady}
