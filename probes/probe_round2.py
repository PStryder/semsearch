"""Round 2 probes:
A. Paged inventory enumeration via EntryID keyset paging (avoids rowset limit error).
B. Content presence for known files (docx/pdf/txt) in root and in C:\\Users Documents.
C. IncludedInCrawlScope URL format variants.
D. IFilter for PDF via direct CoCreateInstance + IPersistFile / IPersistStream, and MTA init.
"""
import glob
import os
import sys
import time
from collections import Counter

import pythoncom
import win32com.client

ROOT = 'file:F:/HexyLab/'


def conn():
    c = win32com.client.Dispatch('ADODB.Connection')
    c.Open("Provider=Search.CollatorDSO;Extended Properties='Application=Windows';")
    return c


def q(c, sql):
    rs = c.Execute(sql)[0]
    rows = []
    while not rs.EOF:
        rows.append([rs.Fields.Item(i).Value for i in range(rs.Fields.Count)])
        rs.MoveNext()
    return rows


def part_a(c):
    print('--- A. paged inventory ---')
    last = 0
    total = 0
    pages = 0
    t0 = time.perf_counter()
    exts = Counter()
    while True:
        rows = q(c, "SELECT TOP 2000 System.Search.EntryID, System.ItemUrl, System.DateModified, System.Size, System.FileExtension, System.IsFolder FROM SystemIndex WHERE SCOPE='%s' AND System.Search.EntryID > %d ORDER BY System.Search.EntryID" % (ROOT, last))
        if not rows:
            break
        pages += 1
        total += len(rows)
        last = rows[-1][0]
        for r in rows:
            if not r[5]:
                exts[(r[4] or '').lower()] += 1
        if pages % 20 == 0:
            print('   page %d total %d (%.1fs)' % (pages, total, time.perf_counter() - t0))
    dt = time.perf_counter() - t0
    print('   total items under root: %d in %d pages, %.1fs (%.0f items/s)' % (total, pages, dt, total / max(dt, 1e-6)))
    print('   top extensions:', exts.most_common(25))


def part_b(c):
    print('--- B. content presence ---')
    tests = [
        ("F:\\HexyLab\\# Compression tastes like red - qua.txt", 'qualia'),
        ("F:\\HexyLab\\Content_Ethics_Whitepaper.docx", 'Symbolic'),
        ("F:\\HexyLab\\2602.12205v2.pdf", 'abstract'),
        ("F:\\HexyLab\\GlyphKeeper 2.docx", 'the'),
    ]
    for path, word in tests:
        rows = q(c, "SELECT System.ItemPathDisplay, System.Search.Rank, System.Search.GatherTime FROM SystemIndex WHERE System.ItemUrl='%s' AND CONTAINS(System.Search.Contents, '%s')" % ('file:' + path.replace('\\', '/'), word))
        meta = q(c, "SELECT System.ItemPathDisplay, System.Search.GatherTime, System.Search.AutoSummary FROM SystemIndex WHERE System.ItemUrl='%s'" % ('file:' + path.replace('\\', '/'),))
        print('   %s CONTAINS(%r): %s | indexed: %s' % (os.path.basename(path), word, bool(rows), meta))
    for scope in ['file:C:/Users/pstry/Documents/', 'file:C:/Users/pstry/Desktop/', 'file:C:/Users/pstry/Downloads/']:
        for ext, word in [('.docx', 'the'), ('.pdf', 'the'), ('.pdf', 'page'), ('.pptx', 'the'), ('.txt', 'the')]:
            rows = q(c, "SELECT TOP 2 System.ItemPathDisplay FROM SystemIndex WHERE SCOPE='%s' AND System.FileExtension='%s' AND CONTAINS(System.Search.Contents, '%s')" % (scope, ext, word))
            n = q(c, "SELECT TOP 1 System.ItemPathDisplay FROM SystemIndex WHERE SCOPE='%s' AND System.FileExtension='%s'" % (scope, ext))
            print('   %s %s CONTAINS(%r): %d hits (type present=%s)' % (scope, ext, word, len(rows), bool(n)))
    # Any docx/pdf anywhere with content?
    for ext in ['.docx', '.pdf', '.xlsx', '.pptx']:
        rows = q(c, "SELECT TOP 3 System.ItemPathDisplay FROM SystemIndex WHERE System.FileExtension='%s' AND CONTAINS(System.Search.Contents, 'the')" % ext)
        print('   ANY %s with content: %s' % (ext, rows))


def part_c():
    print('--- C. IncludedInCrawlScope formats ---')
    sys.path.insert(0, os.path.dirname(__file__))
    import probe_search_api as api
    import comtypes
    mgr = comtypes.CoCreateInstance(api.CLSID_CSearchManager, interface=api.ISearchManager)
    csm = mgr.GetCatalog('SystemIndex').GetCrawlScopeManager()
    for p in [r'file:///F:\HexyLab\pcdc\steering.py', r'file:F:/HexyLab/pcdc/steering.py', r'F:\HexyLab\pcdc\steering.py', r'file:///F:/HexyLab/pcdc/steering.py',
              r'file:///F:\HexyLab\GlyphLite\llama.cpp\README.md', r'file:///C:\Users\pstry\.ssh\id_rsa', r'file:///C:\Users\pstry\AppData\Local\x.txt', r'file:///C:\Users\pstry\Documents\x.txt']:
        try:
            print('   ', p, '->', bool(csm.IncludedInCrawlScope(p)))
        except Exception as e:
            print('   ', p, 'error', e)


def part_d():
    print('--- D. PDF IFilter variants ---')
    import comtypes
    from comtypes import GUID, IUnknown, COMMETHOD, HRESULT
    from comtypes.persist import IPersistFile
    IPersistStream = None
    from ctypes import POINTER, byref, c_wchar_p, oledll, c_ulong, c_void_p, create_unicode_buffer, cast
    import probe_ifilter as pf
    pdf = r'F:\HexyLab\2602.12205v2.pdf'
    # 1) direct CoCreateInstance of the registered PDF filter CLSID, then IPersistFile.Load
    clsid = GUID('{6C337B26-3E38-4F98-813B-FBA18BAB64F5}')
    for ctx_name, ctx in [('INPROC', comtypes.CLSCTX_INPROC_SERVER), ('LOCAL', comtypes.CLSCTX_LOCAL_SERVER)]:
        try:
            unk = comtypes.CoCreateInstance(clsid, interface=IUnknown, clsctx=ctx)
            print('   CoCreateInstance(%s) ok' % ctx_name)
            for name, iface in [('IPersistFile', IPersistFile), ('IFilter', pf.IFilter)]:
                try:
                    unk.QueryInterface(iface)
                    print('      QI %s ok' % name)
                except Exception as e:
                    print('      QI %s failed: %s' % (name, e))
            try:
                pfile = unk.QueryInterface(IPersistFile)
                pfile.Load(pdf, 0)
                flt = unk.QueryInterface(pf.IFilter)
                flt.Init(pf.IFILTER_INIT_INDEXING_ONLY, 0, None)
                # pull first chunk of text
                stat = pf.STAT_CHUNK()
                hr = flt._IFilter__com_GetChunk(byref(stat)) & 0xffffffff
                print('      IPersistFile.Load + Init ok; first GetChunk hr=0x%08x flags=%d' % (hr, stat.flags))
                if hr == 0 and stat.flags & pf.CHUNK_TEXT:
                    n = c_ulong(4096); buf = create_unicode_buffer(4096)
                    hr = flt._IFilter__com_GetText(byref(n), cast(buf, c_void_p)) & 0xffffffff
                    print('      GetText hr=0x%08x: %r' % (hr, buf.value[:200]))
            except Exception as e:
                print('      persist/filter path failed: %s' % e)
            break
        except Exception as e:
            print('   CoCreateInstance(%s) failed: %s' % (ctx_name, e))
    # 2) LoadIFilterEx? (not exported broadly) - try BindIFilterFromStorage/Stream via query.dll
    try:
        q = oledll.query
        names = [n for n in dir(q)]
        print('   query.dll exports probed via ctypes (attribute access only):', [n for n in ('LoadIFilter', 'LoadIFilterEx', 'BindIFilterFromStream', 'BindIFilterFromStorage') if hasattr(q, n)])
    except Exception as e:
        print('   query.dll probe error', e)
    # 3) pypdf fallback timing
    try:
        import pypdf
        t0 = time.perf_counter()
        r = pypdf.PdfReader(pdf)
        txt = ''.join((pg.extract_text() or '') for pg in r.pages[:5])
        print('   pypdf fallback: %d pages, first 5 pages %d chars in %.0f ms: %r' % (len(r.pages), len(txt), (time.perf_counter() - t0) * 1000, txt[:150]))
    except Exception as e:
        print('   pypdf failed', e)


if __name__ == '__main__':
    pythoncom.CoInitialize()
    which = sys.argv[1:] or ['a', 'b', 'c', 'd']
    c = conn()
    if 'a' in which: part_a(c)
    if 'b' in which: part_b(c)
    if 'c' in which: part_c()
    if 'd' in which: part_d()
