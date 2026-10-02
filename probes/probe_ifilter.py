"""Probe: can we reuse Windows' installed IFilters (the same extractors the indexer uses)
from Python via LoadIFilter (query.dll) + the IFilter COM interface?

Read-only: opens files for read only.
"""
import glob
import os
import sys
import time
from ctypes import (POINTER, Structure, Union, c_int, c_ulong, c_ulonglong, c_void_p, c_wchar, c_wchar_p,
                    byref, cast, create_unicode_buffer, oledll, windll, sizeof, c_uint32)
from ctypes.wintypes import BOOL, DWORD, ULONG, LPWSTR

import comtypes
from comtypes import COMMETHOD, GUID, HRESULT, IUnknown

# ---- IFilter declarations (filter.h) ----
IFILTER_INIT_CANON_PARAGRAPHS = 1
IFILTER_INIT_HARD_LINE_BREAKS = 2
IFILTER_INIT_CANON_HYPHENS = 4
IFILTER_INIT_CANON_SPACES = 8
IFILTER_INIT_APPLY_INDEX_ATTRIBUTES = 16
IFILTER_INIT_INDEXING_ONLY = 0x40
FILTER_E_END_OF_CHUNKS = 0x80041700
FILTER_E_NO_MORE_TEXT = 0x80041701
FILTER_E_NO_MORE_VALUES = 0x80041702
FILTER_E_NO_TEXT = 0x80041705
FILTER_S_LAST_TEXT = 0x00041709
CHUNK_TEXT = 1
CHUNK_VALUE = 2


class PROPSPEC_U(Union):
    _fields_ = [('propid', c_ulong), ('lpwstr', c_wchar_p)]


class PROPSPEC(Structure):
    _fields_ = [('ulKind', c_ulong), ('u', PROPSPEC_U)]


class FULLPROPSPEC(Structure):
    _fields_ = [('guidPropSet', GUID), ('psProperty', PROPSPEC)]


class STAT_CHUNK(Structure):
    _fields_ = [('idChunk', c_ulong), ('breakType', c_int), ('flags', c_int), ('locale', c_ulong),
                ('attribute', FULLPROPSPEC), ('idChunkSource', c_ulong), ('cwcStartSource', c_ulong), ('cwcLenSource', c_ulong)]


class IFilter(IUnknown):
    _iid_ = GUID('{89BCB740-6119-101A-BCB7-00DD010655AF}')
    _methods_ = [
        COMMETHOD([], c_int, 'Init', (['in'], c_ulong, 'grfFlags'), (['in'], c_ulong, 'cAttributes'),
                  (['in'], POINTER(FULLPROPSPEC), 'aAttributes'), (['out'], POINTER(c_ulong), 'pFlags')),
        COMMETHOD([], c_int, 'GetChunk', (['out'], POINTER(STAT_CHUNK), 'pStat')),
        COMMETHOD([], c_int, 'GetText', (['in', 'out'], POINTER(c_ulong), 'pcwcBuffer'), (['in'], c_void_p, 'awcBuffer')),
        COMMETHOD([], c_int, 'GetValue', (['out'], POINTER(c_void_p), 'ppPropValue')),
        COMMETHOD([], c_int, 'BindRegion', (['in'], c_void_p, 'origPos'), (['in'], POINTER(GUID), 'riid'), (['out'], POINTER(c_void_p), 'ppunk')),
    ]


def extract(path, max_chars=400):
    q = oledll.query
    pfilt = POINTER(IFilter)()
    hr = q.LoadIFilter(c_wchar_p(path), None, byref(pfilt))
    if hr != 0:
        return None, 'LoadIFilter hr=0x%08x' % (hr & 0xffffffff)
    flt = pfilt
    flags = flt.Init(IFILTER_INIT_INDEXING_ONLY | IFILTER_INIT_CANON_PARAGRAPHS | IFILTER_INIT_APPLY_INDEX_ATTRIBUTES, 0, None)
    out = []
    total = 0
    chunks = 0
    buf_n = 8192
    buf = create_unicode_buffer(buf_n)
    while True:
        stat = STAT_CHUNK()
        hr = flt.__com_GetChunk(byref(stat)) if hasattr(flt, '__com_GetChunk') else None
        # comtypes raises on failure HRESULT; use raw vtable call via _com_ methods
        break
    # Use the raw methods to avoid comtypes raising on FILTER_E_* codes
    raw = flt
    while True:
        stat = STAT_CHUNK()
        hr = raw._IFilter__com_GetChunk(byref(stat))
        hr &= 0xffffffff
        if hr == FILTER_E_END_OF_CHUNKS:
            break
        if hr != 0:
            # some filters return other codes for skipped chunks; continue
            if hr in (FILTER_E_NO_TEXT, 0x80041704, 0x80041706, 0x8004170B):
                continue
            return ''.join(out), 'GetChunk hr=0x%08x after %d chunks' % (hr, chunks)
        chunks += 1
        if stat.flags & CHUNK_TEXT:
            while True:
                n = c_ulong(buf_n)
                hr = raw._IFilter__com_GetText(byref(n), cast(buf, c_void_p)) & 0xffffffff
                if hr in (FILTER_E_NO_MORE_TEXT, FILTER_E_NO_TEXT):
                    break
                if hr not in (0, FILTER_S_LAST_TEXT):
                    break
                s = buf.value[:n.value] if n.value < buf_n else buf.value
                total += len(s)
                if sum(len(x) for x in out) < max_chars:
                    out.append(s)
                if hr == FILTER_S_LAST_TEXT:
                    break
    return ''.join(out), 'ok chunks=%d total_chars=%d' % (chunks, total)


def main():
    comtypes.CoInitialize()
    samples = []
    for pat in ['F:/HexyLab/*.pdf', 'F:/HexyLab/*.docx', 'F:/HexyLab/**/*.pptx', 'F:/HexyLab/**/*.xlsx', 'F:/HexyLab/*.txt', 'F:/HexyLab/*.md', 'F:/HexyLab/pcdc/*.py', 'F:/HexyLab/**/*.json', 'F:/HexyLab/**/*.html', 'F:/HexyLab/**/*.xls', 'F:/HexyLab/**/*.doc']:
        hits = glob.glob(pat, recursive=True)
        hits = [h for h in hits if os.path.isfile(h) and os.path.getsize(h) < 20_000_000][:1]
        samples += hits
    for p in samples:
        t0 = time.perf_counter()
        try:
            text, status = extract(p)
        except Exception as e:
            text, status = None, 'EXC %r' % e
        dt = (time.perf_counter() - t0) * 1000
        print('\n### %s [%s] %.0f ms' % (p, status, dt))
        if text:
            print('   ', repr(text[:300]))


if __name__ == '__main__':
    main()
