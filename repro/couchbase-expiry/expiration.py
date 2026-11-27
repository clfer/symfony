# Replays CouchbaseCollectionAdapterTest::testExpiration k1 (expiresAfter(2), sleep(S), expect miss),
# with memcached's main (clock) thread ptrace-stopped from save+1.5 s to save+1.5+STALL s.
import sys, time
sys.path.insert(0, '.')
from cbmc import CB
from testseq_lib import stop, cont
tid, S, STALL, n = int(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3]), int(sys.argv[4])
c = CB(); fails = 0
for i in range(n):
    k = b'exp%d' % i; t0 = time.monotonic(); c.set(k, b'v1', 2)
    end, check = t0 + 1.5 + STALL, t0 + S + 0.005
    if STALL:
        time.sleep(t0 + 1.5 - time.monotonic()); stop(tid)
        if end < check:
            time.sleep(end - time.monotonic()); cont(tid)
    time.sleep(max(0, check - time.monotonic()))
    still = c.hit(k)
    if STALL and end >= check: cont(tid)
    fails += still; time.sleep(1.5)
print('sleep=%g stall=%g: %d/%d still a hit' % (S, STALL, fails, n))
