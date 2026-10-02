"""Read-only access to the Windows Search management API (ISearchManager / ISearchCatalogManager).

Only status-type calls are made. Reset/Reindex/AddRoot are deliberately not exposed.
Interfaces are declared by hand (no type library is registered for searchapi.h).
"""
from __future__ import annotations

import logging
import threading
from ctypes import POINTER, c_int, c_void_p, c_wchar_p
from ctypes.wintypes import BOOL, DWORD

log = logging.getLogger(__name__)

STATUS_NAMES = {0: "idle", 1: "paused", 2: "recovering", 3: "full_crawl", 4: "incremental_crawl", 5: "processing_notifications", 6: "shutting_down"}
PAUSE_REASONS = {0: "none", 1: "high_io", 2: "high_cpu", 3: "high_ntf_rate", 4: "low_battery", 5: "low_memory", 6: "low_disk", 7: "delayed_recovery", 8: "user_active", 9: "external", 10: "upgrading"}
CLSID_CSearchManager = "{7D096C5F-AC08-4F1F-BEB7-5C22C517CE39}"
_lock = threading.Lock()


def _ifaces():
    if hasattr(_ifaces, "cache"):
        return _ifaces.cache
    from comtypes import COMMETHOD, GUID, HRESULT, IUnknown
    LPWSTR = c_wchar_p

    class ISearchCatalogManager(IUnknown):
        _iid_ = GUID("{AB310581-AC80-11D1-8DF3-00C04FB6EF50}")
        _methods_ = [
            COMMETHOD([], HRESULT, "get_Name", (["out"], POINTER(LPWSTR), "v")),
            COMMETHOD([], HRESULT, "GetParameter", (["in"], LPWSTR, "name"), (["out"], POINTER(c_void_p), "pv")),
            COMMETHOD([], HRESULT, "SetParameter", (["in"], LPWSTR, "name"), (["in"], c_void_p, "pv")),
            COMMETHOD([], HRESULT, "GetCatalogStatus", (["out"], POINTER(c_int), "status"), (["out"], POINTER(c_int), "pausedReason")),
            COMMETHOD([], HRESULT, "Reset"),
            COMMETHOD([], HRESULT, "Reindex"),
            COMMETHOD([], HRESULT, "ReindexMatchingURLs", (["in"], LPWSTR, "pattern")),
            COMMETHOD([], HRESULT, "ReindexSearchRoot", (["in"], LPWSTR, "root")),
            COMMETHOD([], HRESULT, "put_ConnectTimeout", (["in"], DWORD, "v")),
            COMMETHOD([], HRESULT, "get_ConnectTimeout", (["out"], POINTER(DWORD), "v")),
            COMMETHOD([], HRESULT, "put_DataTimeout", (["in"], DWORD, "v")),
            COMMETHOD([], HRESULT, "get_DataTimeout", (["out"], POINTER(DWORD), "v")),
            COMMETHOD([], HRESULT, "NumberOfItems", (["out"], POINTER(c_int), "n")),
            COMMETHOD([], HRESULT, "NumberOfItemsToIndex", (["out"], POINTER(c_int), "incremental"), (["out"], POINTER(c_int), "notification"), (["out"], POINTER(c_int), "highPriority")),
            COMMETHOD([], HRESULT, "URLBeingIndexed", (["out"], POINTER(LPWSTR), "url")),
        ]

    class ISearchManager(IUnknown):
        _iid_ = GUID("{AB310581-AC80-11D1-8DF3-00C04FB6EF69}")
        _methods_ = [
            COMMETHOD([], HRESULT, "GetIndexerVersionStr", (["out"], POINTER(LPWSTR), "v")),
            COMMETHOD([], HRESULT, "GetIndexerVersion", (["out"], POINTER(DWORD), "major"), (["out"], POINTER(DWORD), "minor")),
            COMMETHOD([], HRESULT, "GetParameter", (["in"], LPWSTR, "name"), (["out"], POINTER(c_void_p), "pv")),
            COMMETHOD([], HRESULT, "SetParameter", (["in"], LPWSTR, "name"), (["in"], c_void_p, "pv")),
            COMMETHOD([], HRESULT, "get_ProxyName", (["out"], POINTER(LPWSTR), "v")),
            COMMETHOD([], HRESULT, "get_BypassList", (["out"], POINTER(LPWSTR), "v")),
            COMMETHOD([], HRESULT, "SetProxy", (["in"], c_int, "a"), (["in"], BOOL, "b"), (["in"], DWORD, "c"), (["in"], LPWSTR, "d"), (["in"], LPWSTR, "e")),
            COMMETHOD([], HRESULT, "GetCatalog", (["in"], LPWSTR, "name"), (["out"], POINTER(POINTER(ISearchCatalogManager)), "cat")),
        ]

    _ifaces.cache = (ISearchManager, ISearchCatalogManager, GUID)
    return _ifaces.cache


def catalog_status() -> dict:
    """Return indexer version, catalog state, item counts, and the URL being indexed."""
    try:
        import comtypes
        with _lock:
            try:
                comtypes.CoInitialize()
            except OSError:
                pass
            ISearchManager, _, GUID = _ifaces()
            mgr = comtypes.CoCreateInstance(GUID(CLSID_CSearchManager), interface=ISearchManager)
            cat = mgr.GetCatalog("SystemIndex")
            st, reason = cat.GetCatalogStatus()
            inc, notif, hi = cat.NumberOfItemsToIndex()
            try:
                url = cat.URLBeingIndexed()
            except Exception:
                url = None
            return {
                "available": True,
                "indexer_version": mgr.GetIndexerVersionStr(),
                "status": STATUS_NAMES.get(st, str(st)),
                "paused_reason": PAUSE_REASONS.get(reason, str(reason)),
                "items": cat.NumberOfItems(),
                "to_index": {"incremental": inc, "notification": notif, "high_priority": hi},
                "url_being_indexed": url,
            }
    except Exception as e:
        return {"available": False, "error": str(e)}
