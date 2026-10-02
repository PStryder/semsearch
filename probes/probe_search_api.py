"""Probe the Windows Search COM management API (ISearchManager / ISearchCatalogManager /
ISearchCrawlScopeManager) via comtypes with hand-declared interfaces.

Read-only: it never calls Reindex/Reset/AddRoot/SaveAll.
"""
import sys
import time
from ctypes import POINTER, c_int, c_ulong, c_void_p, byref, c_wchar_p
from ctypes.wintypes import BOOL, DWORD

import comtypes
from comtypes import COMMETHOD, GUID, HRESULT, IUnknown, BSTR, CoCreateInstance
from comtypes.automation import VARIANT_BOOL

LPCWSTR = c_wchar_p
LPWSTR = c_wchar_p


class ISearchRoot(IUnknown):
    _iid_ = GUID('{04C18CCF-1F57-4CBD-88CC-3900F5195CE3}')
    _methods_ = [
        COMMETHOD([], HRESULT, 'put_Schedule', (['in'], LPCWSTR, 'v')),
        COMMETHOD([], HRESULT, 'get_Schedule', (['out'], POINTER(LPWSTR), 'v')),
        COMMETHOD([], HRESULT, 'put_RootURL', (['in'], LPCWSTR, 'v')),
        COMMETHOD([], HRESULT, 'get_RootURL', (['out'], POINTER(LPWSTR), 'v')),
        COMMETHOD([], HRESULT, 'put_IsHierarchical', (['in'], BOOL, 'v')),
        COMMETHOD([], HRESULT, 'get_IsHierarchical', (['out'], POINTER(BOOL), 'v')),
        COMMETHOD([], HRESULT, 'put_ProvidesNotifications', (['in'], BOOL, 'v')),
        COMMETHOD([], HRESULT, 'get_ProvidesNotifications', (['out'], POINTER(BOOL), 'v')),
        COMMETHOD([], HRESULT, 'put_UseNotificationsOnly', (['in'], BOOL, 'v')),
        COMMETHOD([], HRESULT, 'get_UseNotificationsOnly', (['out'], POINTER(BOOL), 'v')),
        COMMETHOD([], HRESULT, 'put_EnumerationDepth', (['in'], DWORD, 'v')),
        COMMETHOD([], HRESULT, 'get_EnumerationDepth', (['out'], POINTER(DWORD), 'v')),
        COMMETHOD([], HRESULT, 'put_HostDepth', (['in'], DWORD, 'v')),
        COMMETHOD([], HRESULT, 'get_HostDepth', (['out'], POINTER(DWORD), 'v')),
        COMMETHOD([], HRESULT, 'put_FollowDirectories', (['in'], BOOL, 'v')),
        COMMETHOD([], HRESULT, 'get_FollowDirectories', (['out'], POINTER(BOOL), 'v')),
        COMMETHOD([], HRESULT, 'put_AuthenticationType', (['in'], c_int, 'v')),
        COMMETHOD([], HRESULT, 'get_AuthenticationType', (['out'], POINTER(c_int), 'v')),
        COMMETHOD([], HRESULT, 'put_User', (['in'], LPCWSTR, 'v')),
        COMMETHOD([], HRESULT, 'get_User', (['out'], POINTER(LPWSTR), 'v')),
        COMMETHOD([], HRESULT, 'put_Password', (['in'], LPCWSTR, 'v')),
        COMMETHOD([], HRESULT, 'get_Password', (['out'], POINTER(LPWSTR), 'v')),
    ]


class IEnumSearchRoots(IUnknown):
    _iid_ = GUID('{AB310581-AC80-11D1-8DF3-00C04FB6EF52}')
    _methods_ = [
        COMMETHOD([], HRESULT, 'Next', (['in'], c_ulong, 'celt'),
                  (['out'], POINTER(POINTER(ISearchRoot)), 'rgelt'),
                  (['out'], POINTER(c_ulong), 'pceltFetched')),
        COMMETHOD([], HRESULT, 'Skip', (['in'], c_ulong, 'celt')),
        COMMETHOD([], HRESULT, 'Reset'),
        COMMETHOD([], HRESULT, 'Clone', (['out'], POINTER(c_void_p), 'ppenum')),
    ]


class ISearchScopeRule(IUnknown):
    _iid_ = GUID('{AB310581-AC80-11D1-8DF3-00C04FB6EF53}')
    _methods_ = [
        COMMETHOD([], HRESULT, 'get_PatternOrURL', (['out'], POINTER(LPWSTR), 'v')),
        COMMETHOD([], HRESULT, 'get_IsIncluded', (['out'], POINTER(BOOL), 'v')),
        COMMETHOD([], HRESULT, 'get_IsDefault', (['out'], POINTER(BOOL), 'v')),
        COMMETHOD([], HRESULT, 'get_FollowFlags', (['out'], POINTER(DWORD), 'v')),
    ]


class IEnumSearchScopeRules(IUnknown):
    _iid_ = GUID('{AB310581-AC80-11D1-8DF3-00C04FB6EF54}')
    _methods_ = [
        COMMETHOD([], HRESULT, 'Next', (['in'], c_ulong, 'celt'),
                  (['out'], POINTER(POINTER(ISearchScopeRule)), 'rgelt'),
                  (['out'], POINTER(c_ulong), 'pceltFetched')),
        COMMETHOD([], HRESULT, 'Skip', (['in'], c_ulong, 'celt')),
        COMMETHOD([], HRESULT, 'Reset'),
        COMMETHOD([], HRESULT, 'Clone', (['out'], POINTER(c_void_p), 'ppenum')),
    ]


class ISearchCrawlScopeManager(IUnknown):
    _iid_ = GUID('{AB310581-AC80-11D1-8DF3-00C04FB6EF55}')
    _methods_ = [
        COMMETHOD([], HRESULT, 'AddDefaultScopeRule', (['in'], LPCWSTR, 'url'), (['in'], BOOL, 'inc'), (['in'], DWORD, 'flags')),
        COMMETHOD([], HRESULT, 'AddRoot', (['in'], POINTER(ISearchRoot), 'root')),
        COMMETHOD([], HRESULT, 'RemoveRoot', (['in'], LPCWSTR, 'url')),
        COMMETHOD([], HRESULT, 'EnumerateRoots', (['out'], POINTER(POINTER(IEnumSearchRoots)), 'ppenum')),
        COMMETHOD([], HRESULT, 'AddHierarchicalScope', (['in'], LPCWSTR, 'url'), (['in'], BOOL, 'inc'), (['in'], BOOL, 'default'), (['in'], BOOL, 'override')),
        COMMETHOD([], HRESULT, 'AddUserScopeRule', (['in'], LPCWSTR, 'url'), (['in'], BOOL, 'inc'), (['in'], BOOL, 'override'), (['in'], DWORD, 'flags')),
        COMMETHOD([], HRESULT, 'RemoveScopeRule', (['in'], LPCWSTR, 'rule')),
        COMMETHOD([], HRESULT, 'EnumerateScopeRules', (['out'], POINTER(POINTER(IEnumSearchScopeRules)), 'ppenum')),
        COMMETHOD([], HRESULT, 'IncludedInCrawlScope', (['in'], LPCWSTR, 'url'), (['out'], POINTER(BOOL), 'pfIsIncluded')),
        COMMETHOD([], HRESULT, 'IncludedInCrawlScopeEx', (['in'], LPCWSTR, 'url'), (['out'], POINTER(BOOL), 'pfIsIncluded'), (['out'], POINTER(c_int), 'reason')),
        COMMETHOD([], HRESULT, 'RevertToDefaultScopes'),
        COMMETHOD([], HRESULT, 'SaveAll'),
        COMMETHOD([], HRESULT, 'GetParentScopeVersionId', (['in'], LPCWSTR, 'url'), (['out'], POINTER(c_int), 'plScopeId')),
        COMMETHOD([], HRESULT, 'RemoveDefaultScopeRule', (['in'], LPCWSTR, 'url')),
    ]


class ISearchCatalogManager(IUnknown):
    _iid_ = GUID('{AB310581-AC80-11D1-8DF3-00C04FB6EF50}')
    _methods_ = [
        COMMETHOD([], HRESULT, 'get_Name', (['out'], POINTER(LPWSTR), 'v')),
        COMMETHOD([], HRESULT, 'GetParameter', (['in'], LPCWSTR, 'name'), (['out'], POINTER(c_void_p), 'pv')),
        COMMETHOD([], HRESULT, 'SetParameter', (['in'], LPCWSTR, 'name'), (['in'], c_void_p, 'pv')),
        COMMETHOD([], HRESULT, 'GetCatalogStatus', (['out'], POINTER(c_int), 'status'), (['out'], POINTER(c_int), 'pausedReason')),
        COMMETHOD([], HRESULT, 'Reset'),
        COMMETHOD([], HRESULT, 'Reindex'),
        COMMETHOD([], HRESULT, 'ReindexMatchingURLs', (['in'], LPCWSTR, 'pattern')),
        COMMETHOD([], HRESULT, 'ReindexSearchRoot', (['in'], LPCWSTR, 'root')),
        COMMETHOD([], HRESULT, 'put_ConnectTimeout', (['in'], DWORD, 'v')),
        COMMETHOD([], HRESULT, 'get_ConnectTimeout', (['out'], POINTER(DWORD), 'v')),
        COMMETHOD([], HRESULT, 'put_DataTimeout', (['in'], DWORD, 'v')),
        COMMETHOD([], HRESULT, 'get_DataTimeout', (['out'], POINTER(DWORD), 'v')),
        COMMETHOD([], HRESULT, 'NumberOfItems', (['out'], POINTER(c_int), 'n')),
        COMMETHOD([], HRESULT, 'NumberOfItemsToIndex', (['out'], POINTER(c_int), 'incremental'), (['out'], POINTER(c_int), 'notification'), (['out'], POINTER(c_int), 'highPriority')),
        COMMETHOD([], HRESULT, 'URLBeingIndexed', (['out'], POINTER(LPWSTR), 'url')),
        COMMETHOD([], HRESULT, 'GetURLIndexingState', (['in'], LPCWSTR, 'url'), (['out'], POINTER(DWORD), 'state')),
        COMMETHOD([], HRESULT, 'GetPersistentItemsChangedSink', (['out'], POINTER(c_void_p), 'sink')),
        COMMETHOD([], HRESULT, 'RegisterViewForNotification', (['in'], LPCWSTR, 'view'), (['in'], c_void_p, 'cb'), (['out'], POINTER(DWORD), 'cookie')),
        COMMETHOD([], HRESULT, 'GetItemsChangedSink', (['in'], c_void_p, 'notifyInline'), (['in'], POINTER(GUID), 'riid'), (['out'], POINTER(c_void_p), 'sink'), (['out'], POINTER(GUID), 'guidCatalogResetSignature'), (['out'], POINTER(GUID), 'guidCheckPointSignature'), (['out'], POINTER(DWORD), 'checkpointNumber')),
        COMMETHOD([], HRESULT, 'UnregisterViewForNotification', (['in'], DWORD, 'cookie')),
        COMMETHOD([], HRESULT, 'SetExtensionClusion', (['in'], LPCWSTR, 'ext'), (['in'], BOOL, 'exclude')),
        COMMETHOD([], HRESULT, 'EnumerateExcludedExtensions', (['out'], POINTER(c_void_p), 'penum')),
        COMMETHOD([], HRESULT, 'GetQueryHelper', (['out'], POINTER(c_void_p), 'helper')),
        COMMETHOD([], HRESULT, 'put_DiacriticSensitivity', (['in'], BOOL, 'v')),
        COMMETHOD([], HRESULT, 'get_DiacriticSensitivity', (['out'], POINTER(BOOL), 'v')),
        COMMETHOD([], HRESULT, 'GetCrawlScopeManager', (['out'], POINTER(POINTER(ISearchCrawlScopeManager)), 'csm')),
    ]


class ISearchManager(IUnknown):
    _iid_ = GUID('{AB310581-AC80-11D1-8DF3-00C04FB6EF69}')
    _methods_ = [
        COMMETHOD([], HRESULT, 'GetIndexerVersionStr', (['out'], POINTER(LPWSTR), 'v')),
        COMMETHOD([], HRESULT, 'GetIndexerVersion', (['out'], POINTER(DWORD), 'major'), (['out'], POINTER(DWORD), 'minor')),
        COMMETHOD([], HRESULT, 'GetParameter', (['in'], LPCWSTR, 'name'), (['out'], POINTER(c_void_p), 'pv')),
        COMMETHOD([], HRESULT, 'SetParameter', (['in'], LPCWSTR, 'name'), (['in'], c_void_p, 'pv')),
        COMMETHOD([], HRESULT, 'get_ProxyName', (['out'], POINTER(LPWSTR), 'v')),
        COMMETHOD([], HRESULT, 'get_BypassList', (['out'], POINTER(LPWSTR), 'v')),
        COMMETHOD([], HRESULT, 'SetProxy', (['in'], c_int, 'a'), (['in'], BOOL, 'b'), (['in'], DWORD, 'c'), (['in'], LPCWSTR, 'd'), (['in'], LPCWSTR, 'e')),
        COMMETHOD([], HRESULT, 'GetCatalog', (['in'], LPCWSTR, 'name'), (['out'], POINTER(POINTER(ISearchCatalogManager)), 'cat')),
        COMMETHOD([], HRESULT, 'get_UserAgent', (['out'], POINTER(LPWSTR), 'v')),
        COMMETHOD([], HRESULT, 'put_UserAgent', (['in'], LPCWSTR, 'v')),
        COMMETHOD([], HRESULT, 'get_UseProxy', (['out'], POINTER(c_int), 'v')),
        COMMETHOD([], HRESULT, 'get_LocalBypass', (['out'], POINTER(BOOL), 'v')),
        COMMETHOD([], HRESULT, 'get_PortNumber', (['out'], POINTER(DWORD), 'v')),
    ]


CLSID_CSearchManager = GUID('{7D096C5F-AC08-4F1F-BEB7-5C22C517CE39}')

STATUS = {0: 'IDLE', 1: 'PAUSED', 2: 'RECOVERING', 3: 'FULL_CRAWL', 4: 'INCREMENTAL_CRAWL', 5: 'PROCESSING_NOTIFICATIONS', 6: 'SHUTTING_DOWN'}


def main():
    comtypes.CoInitialize()
    mgr = CoCreateInstance(CLSID_CSearchManager, interface=ISearchManager)
    print('Indexer version:', mgr.GetIndexerVersionStr())
    cat = mgr.GetCatalog('SystemIndex')
    print('Catalog name:', cat.get_Name())
    st, reason = cat.GetCatalogStatus()
    print('Catalog status:', STATUS.get(st, st), 'pausedReason=', reason)
    print('NumberOfItems:', cat.NumberOfItems())
    inc, notif, hi = cat.NumberOfItemsToIndex()
    print('NumberOfItemsToIndex: incremental=%s notification=%s highPriority=%s' % (inc, notif, hi))
    try:
        print('URLBeingIndexed:', cat.URLBeingIndexed())
    except Exception as e:
        print('URLBeingIndexed error:', e)
    for p in [r'file:///F:\HexyLab\semsearch', r'file:///F:\HexyLab\GlyphLite\llama.cpp\README.md', r'file:///C:\Users\pstry\AppData\Local\x.txt']:
        try:
            print('GetURLIndexingState', p, '->', cat.GetURLIndexingState(p))
        except Exception as e:
            print('GetURLIndexingState', p, 'error:', e)
    csm = cat.GetCrawlScopeManager()
    print('\nRoots:')
    en = csm.EnumerateRoots()
    while True:
        try:
            root, n = en.Next(1)
        except Exception as e:
            print('  enum error', e); break
        if not n or not root:
            break
        try:
            print('  ', root.get_RootURL(), 'hier=', root.get_IsHierarchical(), 'notif=', root.get_ProvidesNotifications(), 'notifOnly=', root.get_UseNotificationsOnly())
        except Exception as e:
            print('  root read error', e)
    print('\nScope rules (first 25):')
    en2 = csm.EnumerateScopeRules()
    k = 0
    while k < 25:
        try:
            rule, n = en2.Next(1)
        except Exception as e:
            print('  enum error', e); break
        if not n or not rule:
            break
        print('  ', rule.get_PatternOrURL(), 'included=', rule.get_IsIncluded(), 'default=', rule.get_IsDefault())
        k += 1
    print('\nIncludedInCrawlScope:')
    for p in [r'file:///F:\HexyLab\semsearch\x.txt', r'file:///F:\HexyLab\GlyphLite\llama.cpp\README.md', r'file:///C:\Users\pstry\AppData\Local\x.txt', r'file:///C:\Users\pstry\.ssh\id_rsa', r'file:///F:\HexyLab\pcdc\steering.py']:
        try:
            inc, reason = csm.IncludedInCrawlScopeEx(p)
            print('  ', p, '->', bool(inc), 'reason=', reason)
        except Exception as e:
            print('  ', p, 'error:', e)


if __name__ == '__main__':
    main()
