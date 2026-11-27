# Replays AdapterTestCase::testDefaultLifeTime (TTL 2, sleep 1, hit, sleep 2, miss expected).
# With --stall, ptrace-stops ONLY memcached's main thread (the one running the 1 s
# mc_time_clock_tick libevent timer) from t0+1.5 s to just after the final GET.
import sys, time, ctypes, os
sys.path.insert(0, '.')
from cbmc import CB
libc = ctypes.CDLL(None, use_errno=True)
def stop(tid):
    assert libc.ptrace(0x4206, tid, 0, 0) == 0, ctypes.get_errno()   # PTRACE_SEIZE
    assert libc.ptrace(0x4207, tid, 0, 0) == 0                       # PTRACE_INTERRUPT
    st = ctypes.c_int(); libc.waitpid(tid, ctypes.byref(st), 0x40000000)  # __WALL
def cont(tid): assert libc.ptrace(17, tid, 0, 0) == 0                 # PTRACE_DETACH
tid = int(sys.argv[1]); stall = '--stall' in sys.argv
c = CB(); fails = 0; n = int(sys.argv[2])
for i in range(n):
    k = b'dlt%d%d' % (stall, i)
    t0 = time.monotonic(); c.set(k, b'value', 2)
    time.sleep(1); assert c.hit(k)
    if stall:
        time.sleep(t0 + 1.5 - time.monotonic()); stop(tid)
    time.sleep(t0 + 3.005 - time.monotonic())
    still = c.hit(k)
    if stall: print(i, still, open("/proc/%d/task/%d/stat" % (tid, tid)).read().split()[2], flush=True)
    if stall: cont(tid); time.sleep(1.5)  # let the clock catch up before the next save
    fails += still
print('stall=%s: %d/%d "Failed asserting that true is false"' % (stall, fails, n))
