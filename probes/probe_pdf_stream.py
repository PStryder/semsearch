"""PDF filter: the registered Windows.Data.Pdf 'Reader Search Handler' supports IFilter but not
IPersistFile. Shell-era filters are initialized via IInitializeWithStream. Test that path.
"""
import os
import sys
import time
from ctypes import POINTER, byref, c_ulong, c_void_p, cast, create_unicode_buffer, oledll, windll, c_wchar_p
from ctypes.wintypes import DWORD

sys.path.insert(0, os.path.dirname(__file__))
import comtypes
from comtypes import COMMETHOD, GUID, HRESULT, IUnknown
import probe_ifilter as pf


class IInitializeWithStream(IUnknown):
    _iid_ = GUID('{B824B49D-22AC-4161-AC8A-9916E8FA3F7F}')
    _methods_ = [COMMETHOD([], HRESULT, 'Initialize', (['in'], c_void_p, 'pstream'), (['in'], DWORD, 'grfMode'))]


class IInitializeWithFile(IUnknown):
    _iid_ = GUID('{B7D14566-0509-4CCE-A71F-0A554233BD9B}')
    _methods_ = [COMMETHOD([], HRESULT, 'Initialize', (['in'], c_wchar_p, 'path'), (['in'], DWORD, 'grfMode'))]


def pull_text(flt, max_chars=300):
    out, total, chunks = [], 0, 0
    buf_n = 8192
    buf = create_unicode_buffer(buf_n)
    while True:
        stat = pf.STAT_CHUNK()
        hr = flt._IFilter__com_GetChunk(byref(stat)) & 0xffffffff
        if hr == pf.FILTER_E_END_OF_CHUNKS:
            break
        if hr != 0:
            if hr in (pf.FILTER_E_NO_TEXT, 0x80041704, 0x80041706, 0x8004170B):
                continue
            return ''.join(out), 'GetChunk hr=0x%08x after %d chunks' % (hr, chunks)
        chunks += 1
        if stat.flags & pf.CHUNK_TEXT:
            while True:
                n = c_ulong(buf_n)
                hr = flt._IFilter__com_GetText(byref(n), cast(buf, c_void_p)) & 0xffffffff
                if hr not in (0, pf.FILTER_S_LAST_TEXT):
                    break
                s = buf.value[:n.value]
                total += len(s)
                if sum(len(x) for x in out) < max_chars:
                    out.append(s)
                if hr == pf.FILTER_S_LAST_TEXT:
                    break
    return ''.join(out), 'ok chunks=%d total_chars=%d' % (chunks, total)


def main():
    comtypes.CoInitialize()
    pdf = sys.argv[1] if len(sys.argv) > 1 else r'F:\HexyLab\2602.12205v2.pdf'
    clsid = GUID('{6C337B26-3E38-4F98-813B-FBA18BAB64F5}')
    unk = comtypes.CoCreateInstance(clsid, interface=IUnknown, clsctx=comtypes.CLSCTX_INPROC_SERVER)
    for name, iface in [('IInitializeWithStream', IInitializeWithStream), ('IInitializeWithFile', IInitializeWithFile)]:
        try:
            unk.QueryInterface(iface)
            print('QI %s ok' % name)
        except Exception as e:
            print('QI %s failed: %s' % (name, e))
    # SHCreateStreamOnFileEx -> IStream*
    pstm = c_void_p()
    STGM_READ = 0
    STGM_SHARE_DENY_NONE = 0x40
    hr = oledll.shlwapi.SHCreateStreamOnFileEx(c_wchar_p(pdf), STGM_READ | STGM_SHARE_DENY_NONE, 0x80, False, None, byref(pstm))
    print('SHCreateStreamOnFileEx hr=%d stream=%s' % (hr, pstm.value is not None))
    t0 = time.perf_counter()
    init = unk.QueryInterface(IInitializeWithStream)
    init.Initialize(pstm, STGM_READ)
    flt = unk.QueryInterface(pf.IFilter)
    flt.Init(pf.IFILTER_INIT_INDEXING_ONLY | pf.IFILTER_INIT_CANON_PARAGRAPHS | pf.IFILTER_INIT_APPLY_INDEX_ATTRIBUTES, 0, None)
    text, status = pull_text(flt)
    print('IInitializeWithStream path: %s in %.0f ms' % (status, (time.perf_counter() - t0) * 1000))
    print('   ', repr(text[:300]))


if __name__ == '__main__':
    main()
