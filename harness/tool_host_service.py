"""In-place ToolHost handoff while the existing desktop owns learning workers."""
import argparse
import ctypes
import os
from pathlib import Path
from tool_host import ToolHost


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--parent', required=True, type=int)
    parser.add_argument('--port', required=True, type=int)
    args = parser.parse_args()
    root = Path(os.environ['FISH_HARNESS_HOME'])
    host = ToolHost(root)
    # The current GUI already owns these workers. Do not start duplicates.
    host.serve(background_workers=False, listen_port=args.port)
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x00100000, False, args.parent)
    try:
        while handle and kernel.WaitForSingleObject(handle, 1000) == 258:
            pass
    finally:
        host.stop.set()
        host.close_instances()
        host.server.shutdown()
        host.server.server_close()
        if handle:
            kernel.CloseHandle(handle)


if __name__ == '__main__':
    main()
