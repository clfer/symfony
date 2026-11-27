# Couchbase 6.5.1 expiry-tick repro (fix #15)

Setup: `docker run -d --name cb --network host couchbase:6.5.1`, then the "Configure Couchbase" curl calls of
`.github/workflows/integration-tests.yml` (ephemeral bucket `cache`). Run the scripts with `python3 -I` (as root,
for ptrace). `<tid>` = host PID of the container's `memcached` process (`docker top cb`); its main thread runs the
1 s `mc_time_clock_tick` timer.

- `lifetime.py N`: when does a TTL-2 item disappear? Result: 2.009-2.997 s over 40 items (depends on tick phase).
- `testseq.py <tid> N [--stall]`: replays `AdapterTestCase::testDefaultLifeTime`. 0/10 failures; with the clock
  thread stopped from save+1.5 s to after the last GET: 10/10 "still a hit" (the CI message).
- `expiration.py <tid> SLEEP STALL N`: replays the Couchbase `testExpiration` k1 check. Stall 1.6 s:
  sleep 3 → 5/5 fail, sleep 4 → 0/5.
