"""Summarize C:\\ProgramData\\SemSearchProbe\\report-*.json into a Markdown comparison table."""
import glob
import json
import os
import sys

BASE = r"C:\ProgramData\SemSearchProbe"
ORDER = ["interactive", "LocalSystem", "LocalService", "NetworkService", "VirtualAccount"]


def cell(c, fmt=lambda v: v):
    if c is None:
        return "n/a"
    if not isinstance(c, dict) or "ok" not in c:
        return str(c)
    if not c["ok"]:
        return "FAIL: " + c["error"].split(":")[0][:40]
    return str(fmt(c["value"]))


def main():
    reps = {}
    for p in glob.glob(os.path.join(BASE, "report-*.json")):
        tag = os.path.basename(p)[len("report-"):-len(".json")]
        reps[tag] = json.load(open(p, encoding="utf-8"))
    tags = [t for t in ORDER if t in reps] + sorted(t for t in reps if t not in ORDER)
    rows = []

    def add(label, fn):
        rows.append([label] + [fn(reps[t]) if "fatal" not in reps[t] or fn.__name__ == "ident" else "FATAL" for t in tags])

    def ident(r):
        i = r.get("identity", {})
        v = i.get("value", {}) if isinstance(i, dict) else {}
        return f"{v.get('domain_user', '?')} s{v.get('session', '?')}"
    ident.__name__ = "ident"
    add("identity / session", ident)
    add("HKCU readable", lambda r: cell(r.get("hkcu"), lambda v: "yes"))
    for root in [r"F:\HexyLab\semsearch\eval\corpus", r"C:\Users\pstry\Documents", r"F:\Personal OneDrive\OneDrive", r"F:\HexyLab"]:
        add(f"fs read {root}", lambda r, root=root: cell(r.get("filesystem", {}).get(root), lambda v: f"{v['entries']} entries"))
    add("Windows Search ping", lambda r: cell(r.get("windows_search", {}).get("ping")))
    add("WS items: eval corpus", lambda r: cell(r.get("windows_search", {}).get("count_eval_corpus")))
    add("WS items: Documents (profile)", lambda r: cell(r.get("windows_search", {}).get("count_documents_profile")))
    add("WS items: OneDrive", lambda r: cell(r.get("windows_search", {}).get("count_onedrive")))
    add("WS FREETEXT", lambda r: cell(r.get("windows_search", {}).get("freetext"), lambda v: f"{len(v)} hits"))
    add("WS catalog COM", lambda r: cell(r.get("windows_search", {}).get("catalog"), lambda v: v.get("status", v.get("error", "?"))))
    add("watcher event", lambda r: cell(r.get("watcher"), lambda v: v["events"][0][0] if v["events"] else "NO EVENT"))
    add("adapters seen", lambda r: ", ".join(f"{a['ordinal']}:{a['name'].split()[-1]}" for a in r.get("adapters", [])) or "none")
    for dev in ["cpu", "dml:0", "dml:1"]:
        add(f"embed {dev} chunks/s", lambda r, dev=dev: cell(r.get("embedding", {}).get(dev), lambda v: v["chunks_per_s"]))
    add("IFilter docx (in-proc COM)", lambda r: cell(r.get("ifilter_docx"), lambda v: f"{v['status']} {v['chars']}ch"))
    add("isolated extractor child", lambda r: cell(r.get("isolated_extractor"), lambda v: f"{v['status']} {v['chars']}ch"))
    add("SQLite+vec in ProgramData", lambda r: cell(r.get("store"), lambda v: f"{v['journal']} {v['vec']}"))
    out = ["| check | " + " | ".join(tags) + " |", "|---|" + "---|" * len(tags)]
    for row in rows:
        out.append("| " + " | ".join(str(x) for x in row) + " |")
    print("\n".join(out))
    for t in tags:
        if "fatal" in reps[t]:
            print(f"\nFATAL {t}: {reps[t]['fatal'][-400:]}")


if __name__ == "__main__":
    main()
