# lockrepro — hub locking regression test (JVM)

Regression test for the **global-lock bug** fixed in 2.4.54:
`MobileEmbeddedHub.apiRequest` used to be globally `synchronized`, so one
unreachable host's SSH attempt (120s budget × retries) stalled **every**
Direct-mode API request.

The test compiles the **real** `MobileEmbeddedHub` + `MobileSsh*` classes
(copied fresh from `android/app/src/main/`) against minimal Android stubs
and drives the public `apiRequest` surface:

- thread 1: `POST /api/contexts/bh/sync` to `192.0.2.1` (TEST-NET-1
  blackhole — packets dropped, no RST, connect hangs to timeout)
- thread 2: `GET /api/system/health`, then `GET /api/agents`
- PASS = the unrelated requests return instantly while the SSH op is stuck

Layout:

- `tests/` — the test driver(s)
- `stubs/android/**` — minimal JVM stand-ins for `Context`, `AssetManager`,
  `Base64`, `Process`
- `stubs/com/cam/app/MobileCredentialStore.java` — in-memory credential
  store (the real one needs Android Keystore)
- `build/` — generated (real sources copied here, classes, data; gitignored)

Run (needs JDK 17+ and curl):

```sh
android/test/lockrepro/run.sh
# or with a custom JDK:
JAVAC=~/tools/jdk17/bin/javac JAVA=~/tools/jdk17/bin/java android/test/lockrepro/run.sh
```

Expected output ends with
`>>> FIX VERIFIED: unrelated requests proceed during blackhole SSH`.

If this ever prints `>>> FAIL`, a change reintroduced cross-request
blocking — check for new `synchronized` on `apiRequest`/`route`, or SSH
I/O happening outside the per-host `MobileSshPool` locks.
