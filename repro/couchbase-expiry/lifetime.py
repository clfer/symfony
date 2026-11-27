import sys, time, random
sys.path.insert(0, '.')
from cbmc import CB
c = CB(); out = []
for i in range(int(sys.argv[1])):
    time.sleep(random.random()); k = b'lt%d' % i
    t0 = time.monotonic(); c.set(k, b'v', 2)
    time.sleep(1.9)
    while c.hit(k): time.sleep(0.002)
    out.append(time.monotonic() - t0)
out.sort(); print('n=%d min=%.3f max=%.3f' % (len(out), out[0], out[-1])); print(' '.join('%.3f' % x for x in out))
