"""Find a paging strategy for full inventory enumeration that the Windows Search SQL dialect accepts."""
import time
import pythoncom
import win32com.client

ROOT = 'file:F:/HexyLab/'


def conn():
    c = win32com.client.Dispatch('ADODB.Connection')
    c.Open("Provider=Search.CollatorDSO;Extended Properties='Application=Windows';")
    return c


def q(c, sql, limit=None):
    rs = c.Execute(sql)[0]
    rows = []
    while not rs.EOF:
        rows.append([rs.Fields.Item(i).Value for i in range(rs.Fields.Count)])
        rs.MoveNext()
        if limit and len(rows) >= limit:
            break
    return rows


def tryq(c, label, sql):
    t0 = time.perf_counter()
    try:
        rows = q(c, sql)
        print('%-45s OK %d rows %.0f ms  first=%s' % (label, len(rows), (time.perf_counter() - t0) * 1000, rows[:1]))
        return rows
    except Exception as e:
        msg = str(e)
        print('%-45s FAIL %s' % (label, msg[:160]))
        return None


pythoncom.CoInitialize()
c = conn()
tryq(c, 'EntryID > 0, no order', "SELECT TOP 5 System.Search.EntryID, System.ItemUrl FROM SystemIndex WHERE SCOPE='%s' AND System.Search.EntryID > 0" % ROOT)
tryq(c, 'ORDER BY EntryID', "SELECT TOP 5 System.Search.EntryID, System.ItemUrl FROM SystemIndex WHERE SCOPE='%s' ORDER BY System.Search.EntryID" % ROOT)
tryq(c, 'ORDER BY ItemUrl', "SELECT TOP 5 System.Search.EntryID, System.ItemUrl FROM SystemIndex WHERE SCOPE='%s' ORDER BY System.ItemUrl" % ROOT)
tryq(c, 'ItemUrl > keyset', "SELECT TOP 5 System.Search.EntryID, System.ItemUrl FROM SystemIndex WHERE SCOPE='%s' AND System.ItemUrl > 'file:F:/HexyLab/pcdc/' ORDER BY System.ItemUrl" % ROOT)
tryq(c, 'ORDER BY GatherTime', "SELECT TOP 5 System.Search.EntryID, System.ItemUrl, System.Search.GatherTime FROM SystemIndex WHERE SCOPE='%s' ORDER BY System.Search.GatherTime" % ROOT)
tryq(c, 'GatherTime keyset', "SELECT TOP 5 System.ItemUrl, System.Search.GatherTime FROM SystemIndex WHERE SCOPE='%s' AND System.Search.GatherTime > '2026-03-13 23:17:09' ORDER BY System.Search.GatherTime" % ROOT)
tryq(c, 'DIRECTORY (shallow)', "SELECT TOP 5 System.ItemUrl, System.IsFolder FROM SystemIndex WHERE DIRECTORY='%s'" % ROOT)
tryq(c, 'folders only via ItemType', "SELECT TOP 5 System.ItemUrl FROM SystemIndex WHERE SCOPE='%s' AND System.ItemType='Directory'" % ROOT)
tryq(c, 'IsFolder=1 (bool literal)', "SELECT TOP 5 System.ItemUrl FROM SystemIndex WHERE SCOPE='%s' AND System.IsFolder=1" % ROOT)
tryq(c, 'FileAttributes dir bit', "SELECT TOP 5 System.ItemUrl FROM SystemIndex WHERE SCOPE='%s' AND System.FileAttributes & 16 = 16" % ROOT)

# large unordered fetch: where does it break? count rows until error
for label, sql in [('no TOP, unordered, 3 cols', "SELECT System.Search.EntryID, System.ItemUrl, System.DateModified FROM SystemIndex WHERE SCOPE='%s'" % ROOT),
                   ('TOP 100000 unordered', "SELECT TOP 100000 System.Search.EntryID, System.ItemUrl, System.DateModified FROM SystemIndex WHERE SCOPE='%s'" % ROOT)]:
    t0 = time.perf_counter()
    try:
        rs = c.Execute(sql)[0]
        n = 0
        while not rs.EOF:
            n += 1
            rs.MoveNext()
        print('%-45s OK %d rows %.1fs' % (label, n, time.perf_counter() - t0))
    except Exception as e:
        print('%-45s FAIL after %d rows %.1fs: %s' % (label, n if 'n' in dir() else -1, time.perf_counter() - t0, str(e)[:120]))

# ADO client cursor variant
try:
    rs = win32com.client.Dispatch('ADODB.Recordset')
    rs.CursorLocation = 3  # adUseClient
    t0 = time.perf_counter()
    rs.Open("SELECT System.Search.EntryID, System.ItemUrl, System.DateModified FROM SystemIndex WHERE SCOPE='%s'" % ROOT, c, 3, 1)
    n = 0
    while not rs.EOF:
        n += 1
        rs.MoveNext()
    print('%-45s OK %d rows %.1fs' % ('client cursor, no TOP', n, time.perf_counter() - t0))
except Exception as e:
    print('%-45s FAIL: %s' % ('client cursor, no TOP', str(e)[:160]))
