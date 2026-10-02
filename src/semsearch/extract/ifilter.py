"""Windows IFilter extractor: reuses the text filters the Windows indexer uses.

Two load paths, tried in order:
  1. ``LoadIFilter`` (query.dll) which handles IPersistFile-style filters (Office, RTF, HTML)
  2. look up the PersistentHandler -> IFilter CLSID in the registry, CoCreateInstance it, and
     initialize via ``IInitializeWithStream`` (needed for the Windows.Data.Pdf filter)

The filter runs in-process. A misbehaving filter can hang or crash the process, which is
why the registry orders this extractor after the pure-Python ones for PDF, and why
``extract_timeout_s`` is enforced by the indexer around every extraction.
"""
from __future__ import annotations

import logging
import threading
import winreg
from ctypes import POINTER, Structure, Union, byref, c_int, c_ulong, c_void_p, c_wchar_p, cast, create_unicode_buffer, oledll
from ctypes.wintypes import DWORD

from ..models import ExtractResult

log = logging.getLogger(__name__)

IID_IFilter = "{89BCB740-6119-101A-BCB7-00DD010655AF}"
IFILTER_INIT_CANON_PARAGRAPHS = 1
IFILTER_INIT_APPLY_INDEX_ATTRIBUTES = 16
IFILTER_INIT_INDEXING_ONLY = 0x40
FILTER_E_END_OF_CHUNKS = 0x80041700
FILTER_E_NO_MORE_TEXT = 0x80041701
FILTER_E_NO_TEXT = 0x80041705
FILTER_S_LAST_TEXT = 0x00041709
_SKIP_CHUNK_HRS = {FILTER_E_NO_TEXT, 0x80041704, 0x80041706, 0x8004170B, 0x80041703}
CHUNK_TEXT = 1
CHUNK_EOP = 2  # breakType paragraph
_tls = threading.local()


def _com_init():
    if getattr(_tls, "init", False):
        return
    import comtypes
    try:
        comtypes.CoInitialize()
    except OSError:
        pass
    _tls.init = True


def _declare():
    """Declare comtypes interfaces lazily (import cost, and keeps non-Windows imports clean)."""
    if hasattr(_declare, "cache"):
        return _declare.cache
    from comtypes import COMMETHOD, GUID, HRESULT, IUnknown

    class PROPSPEC_U(Union):
        _fields_ = [("propid", c_ulong), ("lpwstr", c_wchar_p)]

    class PROPSPEC(Structure):
        _fields_ = [("ulKind", c_ulong), ("u", PROPSPEC_U)]

    class FULLPROPSPEC(Structure):
        _fields_ = [("guidPropSet", GUID), ("psProperty", PROPSPEC)]

    class STAT_CHUNK(Structure):
        _fields_ = [("idChunk", c_ulong), ("breakType", c_int), ("flags", c_int), ("locale", c_ulong),
                    ("attribute", FULLPROPSPEC), ("idChunkSource", c_ulong), ("cwcStartSource", c_ulong), ("cwcLenSource", c_ulong)]

    class IFilter(IUnknown):
        _iid_ = GUID(IID_IFilter)
        _methods_ = [
            COMMETHOD([], c_int, "Init", (["in"], c_ulong, "grfFlags"), (["in"], c_ulong, "cAttributes"),
                      (["in"], POINTER(FULLPROPSPEC), "aAttributes"), (["out"], POINTER(c_ulong), "pFlags")),
            COMMETHOD([], c_int, "GetChunk", (["out"], POINTER(STAT_CHUNK), "pStat")),
            COMMETHOD([], c_int, "GetText", (["in", "out"], POINTER(c_ulong), "pcwcBuffer"), (["in"], c_void_p, "awcBuffer")),
            COMMETHOD([], c_int, "GetValue", (["out"], POINTER(c_void_p), "ppPropValue")),
            COMMETHOD([], c_int, "BindRegion", (["in"], c_void_p, "origPos"), (["in"], POINTER(GUID), "riid"), (["out"], POINTER(c_void_p), "ppunk")),
        ]

    class IInitializeWithStream(IUnknown):
        _iid_ = GUID("{B824B49D-22AC-4161-AC8A-9916E8FA3F7F}")
        _methods_ = [COMMETHOD([], HRESULT, "Initialize", (["in"], c_void_p, "pstream"), (["in"], DWORD, "grfMode"))]

    _declare.cache = (IFilter, IInitializeWithStream, STAT_CHUNK, GUID, IUnknown)
    return _declare.cache


def registered_filter_clsid(extension: str) -> str | None:
    """Follow HKCR\\.ext -> PersistentHandler -> PersistentAddinsRegistered -> IFilter CLSID."""
    ext = extension.lower()
    try:
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, f"{ext}\\PersistentHandler") as k:
                handler = winreg.QueryValue(k, None)
        except OSError:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, ext) as k:
                progid = winreg.QueryValue(k, None)
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, f"{progid}\\PersistentHandler") as k:
                handler = winreg.QueryValue(k, None)
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, f"CLSID\\{handler}\\PersistentAddinsRegistered\\{IID_IFilter}") as k:
            return winreg.QueryValue(k, None)
    except OSError:
        return None


def has_registered_filter(extension: str) -> bool:
    clsid = registered_filter_clsid(extension)
    return bool(clsid) and clsid.lower() != "{098f2470-bae0-11cd-b579-08002b30bfeb}"  # null filter


class IFilterExtractor:
    name = "ifilter"

    def __init__(self, extensions: set[str] | None = None, max_chars: int = 2_000_000):
        self.max_chars = max_chars
        self._ext = {e.lower() for e in extensions} if extensions else None
        self._cache: dict[str, bool] = {}

    def supports(self, extension: str) -> bool:
        ext = extension.lower()
        if self._ext is not None and ext not in self._ext:
            return False
        if ext not in self._cache:
            self._cache[ext] = has_registered_filter(ext)
        return self._cache[ext]

    def _load_via_loadifilter(self, path: str):
        IFilter, _, _, _, _ = _declare()
        p = POINTER(IFilter)()
        hr = oledll.query.LoadIFilter(c_wchar_p(path), None, byref(p))
        if hr != 0 or not p:
            return None
        return p

    def _load_via_stream(self, path: str, extension: str):
        IFilter, IInitializeWithStream, _, GUID, IUnknown = _declare()
        import comtypes
        clsid = registered_filter_clsid(extension)
        if not clsid:
            return None
        unk = comtypes.CoCreateInstance(GUID(clsid), interface=IUnknown, clsctx=comtypes.CLSCTX_INPROC_SERVER)
        pstm = c_void_p()
        STGM_READ, STGM_SHARE_DENY_NONE = 0, 0x40
        hr = oledll.shlwapi.SHCreateStreamOnFileEx(c_wchar_p(path), STGM_READ | STGM_SHARE_DENY_NONE, 0x80, False, None, byref(pstm))
        if hr != 0:
            return None
        init = unk.QueryInterface(IInitializeWithStream)
        init.Initialize(pstm, STGM_READ)
        flt = unk.QueryInterface(IFilter)
        flt._semsearch_stream = pstm  # keep alive
        return flt

    def extract(self, path: str, extension: str) -> ExtractResult:
        _com_init()
        IFilter, _, STAT_CHUNK, _, _ = _declare()
        flt = None
        method = "ifilter:LoadIFilter"
        try:
            flt = self._load_via_loadifilter(path)
        except Exception as e:
            log.debug("LoadIFilter failed for %s: %s", path, e)
        if flt is None:
            try:
                flt = self._load_via_stream(path, extension)
                method = "ifilter:IInitializeWithStream"
            except Exception as e:
                return ExtractResult("", "error", self.name, error=f"filter load failed: {e}")
        if flt is None:
            return ExtractResult("", "unsupported", self.name, error="no IFilter could be loaded")
        try:
            flt.Init(IFILTER_INIT_INDEXING_ONLY | IFILTER_INIT_CANON_PARAGRAPHS | IFILTER_INIT_APPLY_INDEX_ATTRIBUTES, 0, None)
        except Exception as e:
            return ExtractResult("", "error", self.name, error=f"IFilter.Init failed: {e}")
        get_chunk = flt._IFilter__com_GetChunk
        get_text = flt._IFilter__com_GetText
        buf_n = 16384
        buf = create_unicode_buffer(buf_n)
        out: list[str] = []
        total = 0
        chunks = 0
        while total < self.max_chars:
            stat = STAT_CHUNK()
            hr = get_chunk(byref(stat)) & 0xFFFFFFFF
            if hr == FILTER_E_END_OF_CHUNKS:
                break
            if hr != 0:
                if hr in _SKIP_CHUNK_HRS:
                    continue
                if chunks == 0:
                    return ExtractResult("", "error", self.name, error=f"GetChunk hr=0x{hr:08x}")
                break
            chunks += 1
            if not (stat.flags & CHUNK_TEXT):
                continue
            if stat.breakType >= CHUNK_EOP and out and not out[-1].endswith("\n"):
                out.append("\n")
            while True:
                n = c_ulong(buf_n)
                hr = get_text(byref(n), cast(buf, c_void_p)) & 0xFFFFFFFF
                if hr not in (0, FILTER_S_LAST_TEXT):
                    break
                s = buf.value[: n.value]
                if s:
                    out.append(s)
                    total += len(s)
                if hr == FILTER_S_LAST_TEXT:
                    break
        text = "".join(out)
        return ExtractResult(text, "ok" if text.strip() else "empty", method, meta={"chunks": chunks})
