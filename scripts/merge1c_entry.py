"""Entry point for the Windows distribution; pip uses project.scripts instead."""

from merge1c.cli import main


if __name__ == "__main__":
    # PyInstaller's DLL search directory is inherited by Git and validators.
    # All application imports are loaded; children must use their own DLLs.
    import sys

    if sys.platform == "win32" and getattr(sys, "frozen", False):
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.SetDllDirectoryW.argtypes = [wintypes.LPCWSTR]
        kernel32.SetDllDirectoryW.restype = wintypes.BOOL
        if not kernel32.SetDllDirectoryW(None):
            raise ctypes.WinError(ctypes.get_last_error())
    raise SystemExit(main())
