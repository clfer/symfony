import ctypes
libc = ctypes.CDLL(None, use_errno=True)
def stop(tid):
    assert libc.ptrace(0x4206, tid, 0, 0) == 0, ctypes.get_errno()   # PTRACE_SEIZE
    assert libc.ptrace(0x4207, tid, 0, 0) == 0                       # PTRACE_INTERRUPT
    st = ctypes.c_int(); libc.waitpid(tid, ctypes.byref(st), 0x40000000)  # __WALL
def cont(tid): assert libc.ptrace(17, tid, 0, 0) == 0                 # PTRACE_DETACH
