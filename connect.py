import sys

import pywintypes
import win32com.client


MK_E_UNAVAILABLE = -2147221021


def connect():
    try:
        # Attach to a running SOLIDWORKS session when one is available.
        sw_app = win32com.client.GetActiveObject("SldWorks.Application")
        action = "Connected to the running SOLIDWORKS session."
    except pywintypes.com_error as error:
        if error.hresult != MK_E_UNAVAILABLE:
            raise
        # No accessible running instance: ask COM to start SOLIDWORKS.
        print("No accessible SOLIDWORKS session found. Starting SOLIDWORKS...")
        sw_app = win32com.client.Dispatch("SldWorks.Application")
        action = "Connected to SOLIDWORKS through COM activation."

    sw_app.Visible = True
    # COM wrappers can expose RevisionNumber as a value or a method.
    revision = sw_app.RevisionNumber
    version = revision() if callable(revision) else revision
    # Keep the application open after this standalone script finishes.
    sw_app.UserControl = True
    print(action)
    print("SOLIDWORKS revision:", version)
    return sw_app


if __name__ == "__main__":
    try:
        swApp = connect()
    except pywintypes.com_error as error:
        print("Failed to connect to SOLIDWORKS.", file=sys.stderr)
        print(f"COM error 0x{error.hresult & 0xFFFFFFFF:08X}: {error}", file=sys.stderr)
        print(
            "Check that SOLIDWORKS is installed and can open manually. "
            "Run SOLIDWORKS and this terminal under the same Windows user "
            "and with the same administrator privileges.",
            file=sys.stderr,
        )
        sys.exit(1)
