# Minimal memcached-binary-protocol client for Couchbase 6.5 KV (port 11210).
import socket, struct, zlib, time
class CB:
    def __init__(s, host='127.0.0.1', user='Administrator', pw='111111@', bucket='cache'):
        s.s = socket.create_connection((host, 11210)); s.s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        st, _ = s.req(0x21, key=b'PLAIN', val=b'\0' + user.encode() + b'\0' + pw.encode()); assert st == 0, st
        st, _ = s.req(0x89, key=bucket.encode()); assert st == 0, st
    def _recv(s, n):
        b = b''
        while len(b) < n:
            c = s.s.recv(n - len(b)); assert c; b += c
        return b
    def req(s, op, key=b'', extras=b'', val=b'', vb=0):
        h = struct.pack('>BBHBBHIIQ', 0x80, op, len(key), len(extras), 0, vb, len(key)+len(extras)+len(val), 0, 0)
        s.s.sendall(h + extras + key + val)
        r = s._recv(24); _, _, kl, el, _, st, bl, _, _ = struct.unpack('>BBHBBHIIQ', r)
        body = s._recv(bl); return st, body[el+kl:]
    @staticmethod
    def vb(key): return ((zlib.crc32(key) >> 16) & 0x7fff) % 1024
    def set(s, key, val, ttl):
        return s.req(0x01, key, struct.pack('>II', 0, ttl), val, s.vb(key))[0]
    def hit(s, key):
        st, _ = s.req(0x00, key, vb=s.vb(key)); assert st in (0, 1), st; return st == 0
