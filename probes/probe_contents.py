"""Probe: which indexed file types actually have full-text content in SystemIndex,
and how fast / how large are scope enumerations (inventory feasibility).
"""
import time
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


def main():
    c = conn()
    for ext, word in [('.pdf', 'model'), ('.docx', 'ethics'), ('.txt', 'the'), ('.txt', 'memory'), ('.md', 'memory'), ('.py', 'import'), ('.json', 'name'), ('.html', 'div'), ('.pptx', 'slide'), ('.xlsx', 'total'), ('.yaml', 'name'), ('.ps1', 'param'), ('.cs', 'class'), ('.js', 'function')]:
        t0 = time.perf_counter()
        rows = q(c, "SELECT TOP 3 System.ItemPathDisplay, System.Search.Rank FROM SystemIndex WHERE SCOPE='%s' AND System.FileExtension='%s' AND CONTAINS(System.Search.Contents, '%s')" % (ROOT, ext, word))
        n_any = q(c, "SELECT TOP 1 System.ItemPathDisplay FROM SystemIndex WHERE SCOPE='%s' AND System.FileExtension='%s'" % (ROOT, ext))
        print('%-6s CONTAINS(%r): %d hits (%.0f ms) [type present in index: %s] %s' % (ext, word, len(rows), (time.perf_counter()-t0)*1000, bool(n_any), rows[:1]))

    # full inventory enumeration timing under the root
    t0 = time.perf_counter()
    rows = q(c, "SELECT System.ItemUrl, System.Search.EntryID, System.DateModified, System.Size, System.FileExtension, System.ItemType, System.IsFolder FROM SystemIndex WHERE SCOPE='%s'" % ROOT)
    dt = time.perf_counter() - t0
    files = [r for r in rows if r[6] is False or r[6] == 0]
    print('\nInventory under %s: %d items (%d files) in %.2fs' % (ROOT, len(rows), len(files), dt))
    from collections import Counter
    cnt = Counter((r[4] or '').lower() for r in files)
    print('Top extensions:', cnt.most_common(30))

    # filename literal search semantics (Explorer-style)
    t0 = time.perf_counter()
    rows = q(c, "SELECT TOP 10 System.ItemPathDisplay FROM SystemIndex WHERE SCOPE='%s' AND System.FileName LIKE '%%blackboard%%'" % ROOT)
    print('\nFileName LIKE blackboard: %d (%.0f ms) %s' % (len(rows), (time.perf_counter()-t0)*1000, [r[0] for r in rows[:5]]))
    t0 = time.perf_counter()
    rows = q(c, "SELECT TOP 10 System.ItemPathDisplay, System.Search.Rank FROM SystemIndex WHERE SCOPE='%s' AND FREETEXT('destructive actions agents') ORDER BY System.Search.Rank DESC" % ROOT)
    print('FREETEXT destructive actions agents: %d (%.0f ms) %s' % (len(rows), (time.perf_counter()-t0)*1000, rows[:5]))
    rows = q(c, "SELECT TOP 10 System.ItemPathDisplay, System.Search.Rank FROM SystemIndex WHERE SCOPE='%s' AND CONTAINS(*, 'destructive')" % ROOT)
    print('CONTAINS(*, destructive): %d %s' % (len(rows), rows[:5]))
    # GatherTime delta query (incremental hook)
    t0 = time.perf_counter()
    rows = q(c, "SELECT System.ItemUrl, System.Search.GatherTime FROM SystemIndex WHERE SCOPE='%s' AND System.Search.GatherTime >= '2026-09-28 00:00:00'" % ROOT)
    print('GatherTime >= 2026-09-28: %d rows (%.0f ms)' % (len(rows), (time.perf_counter()-t0)*1000))


if __name__ == '__main__':
    main()
