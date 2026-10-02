"""Probe: how does Windows Search expose change over time?

1. Create a file under an indexed scope; measure latency until it appears in SystemIndex;
   record System.Search.EntryID, GatherTime, and NTFS file id (os.stat st_ino).
2. Modify content; check GatherTime advances and EntryID is stable.
3. Rename; check whether EntryID survives (does the indexer treat it as the same item?)
   and whether the NTFS file id survives.
4. Delete; measure removal latency.
5. Try reading the USN journal on F: without elevation.
6. CONTAINS on content for a type with an IFilter (.txt) vs without (.md).

Writes only inside F:\HexyLab\semsearch\_probe (its own directory) and removes it after.
"""
import os
import shutil
import sys
import time
import win32com.client
import pywintypes

PROBE_DIR = r'F:\HexyLab\semsearch\_probe'
MARK = 'zqxv' + str(int(time.time()))  # a token no other file will contain


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


def wait_for(c, sql, want_rows=True, timeout=240):
    t0 = time.time()
    while time.time() - t0 < timeout:
        rows = q(c, sql)
        if bool(rows) == want_rows:
            return rows, time.time() - t0
        time.sleep(1.0)
    return None, time.time() - t0


def url(p):
    return 'file:' + p.replace('\\', '/')


def main():
    os.makedirs(PROBE_DIR, exist_ok=True)
    c = conn()
    p1 = os.path.join(PROBE_DIR, 'probe_a.txt')
    with open(p1, 'w', encoding='utf-8') as f:
        f.write('Agents must never delete user files without a receipt. token ' + MARK + '\n')
    ino1 = os.stat(p1).st_ino
    sel = "SELECT System.ItemPathDisplay, System.Search.EntryID, System.Search.GatherTime, System.DateModified, System.Size FROM SystemIndex WHERE System.ItemUrl='%s'"
    rows, dt = wait_for(c, sel % url(p1))
    print('1. appear latency %.1fs rows=%s st_ino=%s' % (dt, rows, ino1))
    if not rows:
        print('   file never appeared in index; aborting change probe');
    else:
        entry1, gather1 = rows[0][1], rows[0][2]
        # content search on txt (has IFilter)
        rows2, dt2 = wait_for(c, "SELECT System.ItemPathDisplay FROM SystemIndex WHERE CONTAINS(System.Search.Contents, '%s')" % MARK, timeout=120)
        print('   CONTAINS(txt token) latency %.1fs -> %s' % (dt2, rows2))
        # modify
        time.sleep(2)
        with open(p1, 'a', encoding='utf-8') as f:
            f.write('second line appended ' + MARK + 'MOD\n')
        rows, dt = wait_for(c, sel % url(p1) + " AND System.Search.GatherTime > '%s'" % gather1.strftime('%Y-%m-%d %H:%M:%S') if hasattr(gather1, 'strftime') else sel % url(p1), timeout=180)
        print('2. modify re-gather latency %.1fs rows=%s (entry stable=%s)' % (dt, rows, rows and rows[0][1] == entry1))
        # rename
        p2 = os.path.join(PROBE_DIR, 'probe_b_renamed.txt')
        os.rename(p1, p2)
        ino2 = os.stat(p2).st_ino
        rows, dt = wait_for(c, sel % url(p2), timeout=180)
        gone, dtg = wait_for(c, sel % url(p1), want_rows=False, timeout=60)
        print('3. rename: new path visible after %.1fs rows=%s; old path gone=%s after %.1fs; EntryID same=%s; st_ino same=%s' % (dt, rows, gone is not None, dtg, rows and rows[0][1] == entry1, ino1 == ino2))
        # move to subdir
        sub = os.path.join(PROBE_DIR, 'sub')
        os.makedirs(sub, exist_ok=True)
        p3 = os.path.join(sub, 'probe_b_renamed.txt')
        shutil.move(p2, p3)
        rows, dt = wait_for(c, sel % url(p3), timeout=180)
        print('4. move to subdir: visible after %.1fs rows=%s EntryID same=%s st_ino same=%s' % (dt, rows, rows and rows[0][1] == entry1, os.stat(p3).st_ino == ino1))
        # delete
        os.remove(p3)
        gone, dtg = wait_for(c, sel % url(p3), want_rows=False, timeout=120)
        print('5. delete: gone=%s after %.1fs' % (gone is not None, dtg))

    # md (no IFilter) content test
    pmd = os.path.join(PROBE_DIR, 'probe_c.md')
    with open(pmd, 'w', encoding='utf-8') as f:
        f.write('# Markdown probe\n\nblackboard read tracking design token ' + MARK + 'MD\n')
    rows, dt = wait_for(c, sel % url(pmd), timeout=120)
    print('6. md appears after %.1fs: %s' % (dt, rows))
    rows2, dt2 = wait_for(c, "SELECT System.ItemPathDisplay FROM SystemIndex WHERE CONTAINS(System.Search.Contents, '%sMD')" % MARK, timeout=45)
    print('   CONTAINS(md token) within 45s -> %s (None = never; expected: md has no IFilter)' % (rows2,))

    # USN journal read attempt without elevation
    try:
        import win32file, winioctlcon, struct
        h = win32file.CreateFile(r'\\.\F:', win32file.GENERIC_READ, win32file.FILE_SHARE_READ | win32file.FILE_SHARE_WRITE, None, win32file.OPEN_EXISTING, 0, None)
        data = win32file.DeviceIoControl(h, winioctlcon.FSCTL_QUERY_USN_JOURNAL, None, 80)
        jid, first, next_usn = struct.unpack_from('<QQQ', data)
        print('7. USN query ok: journal=%x first=%x next=%x' % (jid, first, next_usn))
        inbuf = struct.pack('<qLLQQQ', 0, 0xFFFFFFFF, 0, 0, 0, jid)  # READ_USN_JOURNAL_DATA_V0: StartUsn, ReasonMask, ReturnOnlyOnClose, Timeout, BytesToWaitFor, UsnJournalID
        try:
            out = win32file.DeviceIoControl(h, winioctlcon.FSCTL_READ_USN_JOURNAL, inbuf, 65536)
            print('   USN read ok without elevation: %d bytes' % len(out))
        except pywintypes.error as e:
            print('   USN read FAILED (expected without elevation): %s' % e)
        h.Close()
    except Exception as e:
        print('7. USN open/query failed: %r' % e)

    shutil.rmtree(PROBE_DIR, ignore_errors=True)
    print('cleaned up', PROBE_DIR)


if __name__ == '__main__':
    main()
