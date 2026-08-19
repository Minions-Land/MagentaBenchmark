# Infrastructure Preflight

```text
[ ] Authorized project and write root confirmed
[ ] Other roots and processes treated as read-only
[ ] Disk free-space floor and output name checked
[ ] Endpoint/model/GPU health checked without changing service state
[ ] Job slots, endpoint quota, GPU capacity, and storage limits recorded
[ ] Environment variable names present; values not printed
[ ] Proxy/no-proxy behavior verified
[ ] Source and dependency revisions recorded
[ ] Adapter is isolated from shared framework
[ ] Smoke and monitoring artifact ready
[ ] Stop, retry, escalation, and ownership rules recorded
```

Report blockers with the exact check, observed safe fact, requested decision,
and whether work is blocked. Do not hide a failed preflight behind a partial
run.
