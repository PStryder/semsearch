"""Windows Search adapter (OLE DB provider ``Search.CollatorDSO`` via ADO).

What it is used for:
  * deep enumeration of a root (``SCOPE='file:...'``) with keyset paging on System.ItemUrl
  * incremental candidates via ``System.Search.GatherTime >= checkpoint``
  * point lookups by ``System.ItemUrl``
  * ``FREETEXT``/``CONTAINS`` relevance (System.Search.Rank) as an optional lexical signal

Facts established in docs/windows-search-findings.md and relied on here:
  * COUNT/GROUP BY are not supported; folders are ``System.ItemType='Directory'``
  * EntryID is not stable across renames; it is recorded but never used as identity
  * ~6k items/s enumeration; point lookup ~10 ms
COM is thread-affine: a connection is created per thread, lazily.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import re
import threading
from typing import Iterator

from ..models import FileEntry
from ..security import display_path, is_excluded

log = logging.getLogger(__name__)

CONN_STR = "Provider=Search.CollatorDSO;Extended Properties='Application=Windows';"
_ENUM_COLS = "System.ItemUrl, System.Search.EntryID, System.DateModified, System.Size, System.FileExtension, System.ItemType, System.Search.GatherTime"
_tls = threading.local()


def path_to_url(path: str) -> str:
    p = display_path(path).replace("\\", "/")
    return "file:" + p


def url_to_path(url: str) -> str:
    s = url
    if s.lower().startswith("file:"):
        s = s[5:]
    s = s.lstrip("/")
    return s.replace("/", "\\")


def _sql_str(s: str) -> str:
    return s.replace("'", "''")


def _to_ts(v) -> float | None:
    if v is None:
        return None
    try:
        if isinstance(v, dt.datetime):
            if v.tzinfo is None:
                return v.replace(tzinfo=dt.timezone.utc).timestamp()
            return v.timestamp()
        return float(v)
    except Exception:
        return None


def _ts_to_sql(ts: float) -> str:
    # Windows Search compares datetimes in UTC
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class WindowsSearchInventory:
    name = "windows_search"

    def __init__(self, roots: list[str], excludes: list[str], page_size: int = 2000):
        self.roots = roots
        self.excludes = excludes
        self.page_size = page_size

    # ---- connection handling ----
    def _conn(self):
        c = getattr(_tls, "conn", None)
        if c is None:
            import pythoncom
            import win32com.client
            try:
                pythoncom.CoInitialize()
            except Exception:
                pass
            c = win32com.client.Dispatch("ADODB.Connection")
            c.CommandTimeout = 120
            c.Open(CONN_STR)
            _tls.conn = c
        return c

    def _query(self, sql: str) -> list[list]:
        c = self._conn()
        try:
            rs = c.Execute(sql)[0]
        except Exception:
            # connection may have gone stale (service restart); rebuild once
            _tls.conn = None
            c = self._conn()
            rs = c.Execute(sql)[0]
        rows: list[list] = []
        try:
            n = rs.Fields.Count
            while not rs.EOF:
                rows.append([rs.Fields.Item(i).Value for i in range(n)])
                rs.MoveNext()
        finally:
            try:
                rs.Close()
            except Exception:
                pass
        return rows

    @staticmethod
    def available() -> bool:
        try:
            import win32com.client  # noqa: F401
            return True
        except Exception:
            return False

    def ping(self) -> bool:
        try:
            self._query("SELECT TOP 1 System.ItemUrl FROM SystemIndex")
            return True
        except Exception as e:
            log.warning("Windows Search OLE DB unavailable: %s", e)
            return False

    # ---- InventorySource ----
    def covers(self, root: str) -> bool:
        """A root is covered if the folder itself is an item in SystemIndex."""
        try:
            rows = self._query(f"SELECT TOP 1 System.ItemUrl FROM SystemIndex WHERE System.ItemUrl='{_sql_str(path_to_url(root))}'")
            if rows:
                return True
            rows = self._query(f"SELECT TOP 1 System.ItemUrl FROM SystemIndex WHERE SCOPE='{_sql_str(path_to_url(root))}/'")
            return bool(rows)
        except Exception as e:
            log.warning("covers(%s) failed: %s", root, e)
            return False

    def _row_to_entry(self, r) -> FileEntry | None:
        url, entry_id, modified, size, ext, item_type, gather = r
        if not url or not str(url).lower().startswith("file:"):
            return None
        path = url_to_path(str(url))
        is_dir = (item_type == "Directory")
        return FileEntry(path=path, size=int(size) if size is not None else None, mtime=_to_ts(modified), is_dir=is_dir,
                         source=self.name, win_entry_id=int(entry_id) if entry_id is not None else None,
                         gather_time=_to_ts(gather), extension=(ext or os.path.splitext(path)[1]).lower())

    def enumerate(self, root: str) -> Iterator[FileEntry]:
        scope = _sql_str(path_to_url(root) + "/")
        last = ""
        while True:
            where = f"SCOPE='{scope}'"
            if last:
                where += f" AND System.ItemUrl > '{_sql_str(last)}'"
            rows = self._query(f"SELECT TOP {self.page_size} {_ENUM_COLS} FROM SystemIndex WHERE {where} ORDER BY System.ItemUrl")
            if not rows:
                return
            for r in rows:
                fe = self._row_to_entry(r)
                if fe is None or fe.is_dir:
                    continue
                if is_excluded(fe.path, self.excludes):
                    continue
                yield fe
            last = str(rows[-1][0])
            if len(rows) < self.page_size:
                return

    def changed_since(self, root: str, since_ts: float) -> Iterator[FileEntry]:
        scope = _sql_str(path_to_url(root) + "/")
        since_sql = _ts_to_sql(max(0.0, since_ts))
        last_url = ""
        while True:
            where = f"SCOPE='{scope}' AND System.Search.GatherTime >= '{since_sql}'"
            if last_url:
                where += f" AND System.ItemUrl > '{_sql_str(last_url)}'"
            rows = self._query(f"SELECT TOP {self.page_size} {_ENUM_COLS} FROM SystemIndex WHERE {where} ORDER BY System.ItemUrl")
            if not rows:
                return
            for r in rows:
                fe = self._row_to_entry(r)
                if fe is None or fe.is_dir or is_excluded(fe.path, self.excludes):
                    continue
                yield fe
            last_url = str(rows[-1][0])
            if len(rows) < self.page_size:
                return

    def lookup(self, path: str) -> FileEntry | None:
        rows = self._query(f"SELECT TOP 1 {_ENUM_COLS} FROM SystemIndex WHERE System.ItemUrl='{_sql_str(path_to_url(path))}'")
        return self._row_to_entry(rows[0]) if rows else None

    # ---- LexicalSource ----
    _WORD = re.compile(r"[\w'-]+", re.UNICODE)

    def freetext(self, query: str, roots: list[str], limit: int = 50) -> list[tuple[str, float]]:
        words = [w for w in self._WORD.findall(query) if len(w) > 1][:20]
        if not words:
            return []
        q = _sql_str(" ".join(words))
        scopes = " OR ".join(f"SCOPE='{_sql_str(path_to_url(r) + '/')}'" for r in roots) or "1=1"
        sql = (f"SELECT TOP {int(limit)} System.ItemUrl, System.Search.Rank FROM SystemIndex "
               f"WHERE ({scopes}) AND FREETEXT('{q}') ORDER BY System.Search.Rank DESC")
        try:
            rows = self._query(sql)
        except Exception as e:
            log.debug("FREETEXT failed: %s", e)
            return []
        out = []
        for url, rank in rows:
            if url and str(url).lower().startswith("file:"):
                out.append((url_to_path(str(url)), float(rank or 0)))
        return out

    def filename_like(self, pattern: str, roots: list[str], limit: int = 100) -> list[str]:
        """Explorer-style filename match; pattern uses * and ? wildcards."""
        like = _sql_str(pattern.replace("%", "[%]").replace("*", "%").replace("?", "_"))
        scopes = " OR ".join(f"SCOPE='{_sql_str(path_to_url(r) + '/')}'" for r in roots) or "1=1"
        sql = f"SELECT TOP {int(limit)} System.ItemUrl FROM SystemIndex WHERE ({scopes}) AND System.FileName LIKE '{like}'"
        try:
            return [url_to_path(str(r[0])) for r in self._query(sql) if r[0]]
        except Exception as e:
            log.debug("filename LIKE failed: %s", e)
            return []
