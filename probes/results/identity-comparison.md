| check | interactive | LocalSystem | LocalService | NetworkService | VirtualAccount |
|---|---|---|---|---|---|
| identity / session | PETERDESKTOP\pstry s1 | WORKGROUP\PETERDESKTOP$ s0 | NT AUTHORITY\LOCAL SERVICE s0 | WORKGROUP\PETERDESKTOP$ s0 | NT SERVICE\SemSearchProbe s0 |
| HKCU readable | yes | FAIL: FileNotFoundError | FAIL: FileNotFoundError | FAIL: FileNotFoundError | FAIL: FileNotFoundError |
| fs read F:\HexyLab\semsearch\eval\corpus | 15 entries | 15 entries | 15 entries | 15 entries | 15 entries |
| fs read C:\Users\pstry\Documents | 65 entries | 65 entries | FAIL: PermissionError | FAIL: PermissionError | FAIL: PermissionError |
| fs read F:\Personal OneDrive\OneDrive | 10 entries | 10 entries | 10 entries | 10 entries | 10 entries |
| fs read F:\HexyLab | 118 entries | 118 entries | 118 entries | 118 entries | 118 entries |
| Windows Search ping | True | True | True | True | True |
| WS items: eval corpus | 0 | 0 | 0 | 0 | 0 |
| WS items: Documents (profile) | 38402 | 38402 | 0 | 0 | 0 |
| WS items: OneDrive | 4873 | 4873 | 4873 | 4873 | 4873 |
| WS FREETEXT | 0 hits | 0 hits | 0 hits | 0 hits | 0 hits |
| WS catalog COM | idle | idle | idle | idle | idle |
| watcher event | changed | changed | changed | changed | changed |
| adapters seen | 0:4080, 1:Graphics, 2:Driver | 0:Graphics, 1:4080, 2:Driver | 0:Graphics, 1:4080, 2:Driver | 0:Graphics, 1:4080, 2:Driver | 0:Graphics, 1:4080, 2:Driver |
| embed cpu chunks/s | 18.3 | 22.4 | 22.4 | 21.7 | 22.6 |
| embed dml:0 chunks/s | 149.7 | 4.8 | 4.9 | 5.1 | 5.0 |
| embed dml:1 chunks/s | 4.8 | 167.8 | 155.1 | 159.2 | 169.2 |
| IFilter docx (in-proc COM) | ok 13048ch | ok 13048ch | ok 13048ch | ok 13048ch | ok 13048ch |
| isolated extractor child | ok 13048ch | ok 13048ch | ok 13048ch | ok 13048ch | ok 13048ch |
| SQLite+vec in ProgramData | wal v0.1.9 | wal v0.1.9 | wal v0.1.9 | wal v0.1.9 | wal v0.1.9 |
