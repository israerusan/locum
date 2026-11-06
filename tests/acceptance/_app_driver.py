"""Child process for the acceptance scenarios: makes a real application block on a dialog.

Run as ``python _app_driver.py <scenario> <workdir>``. It prints one JSON line
``{"event": "ready", "pid": <application pid>}`` once the blocking call is about
to start, then ``{"event": "done", ...}`` with the outcome. The parent finds the
dialog among the windows of that pid and answers it with locum.

Excel scenarios use a private instance (DispatchEx) and always quit it.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time


def emit(**data):
    sys.stdout.write(json.dumps(data) + "\n")
    sys.stdout.flush()


def _excel():
    import win32com.client
    import win32process

    app = win32com.client.DispatchEx("Excel.Application")
    app.Visible = True          # dialogs of an invisible Excel have no window to find
    app.DisplayAlerts = True    # the point: Excel asks
    app.ScreenUpdating = True
    _, pid = win32process.GetWindowThreadProcessId(app.Hwnd)
    return app, pid


def _announce_soon(pid, delay=0.3):
    # The blocking COM call starts right after this; the parent polls for the dialog anyway.
    threading.Timer(delay, lambda: emit(event="ready", pid=pid)).start()


def save_prompt(work):
    """Close a workbook with unsaved changes: Excel asks Save / Don't Save / Cancel."""
    path = os.path.join(work, "save_prompt.xlsx")
    app, pid = _excel()
    try:
        wb = app.Workbooks.Add()
        wb.Worksheets(1).Range("A1").Value = 1
        wb.SaveAs(path, 51)
        wb.Worksheets(1).Range("A1").Value = 2   # unsaved change
        _announce_soon(pid)
        wb.Close()                                # blocks until the dialog is answered
        emit(event="done", open_workbooks=app.Workbooks.Count, path=path)
    finally:
        app.DisplayAlerts = False
        app.Quit()


def overwrite_prompt(work):
    """SaveAs onto an existing file: Excel asks whether to replace it."""
    path = os.path.join(work, "overwrite.xlsx")
    app, pid = _excel()
    try:
        wb = app.Workbooks.Add()
        wb.Worksheets(1).Range("A1").Value = "old"
        wb.SaveAs(path, 51)
        wb.Close(False)
        wb = app.Workbooks.Add()
        wb.Worksheets(1).Range("A1").Value = "new"
        _announce_soon(pid)
        try:
            wb.SaveAs(path, 51)                   # blocks on "already exists... replace?"
            replaced = True
        except Exception:                         # answered No/Cancel
            replaced = False
        wb.Close(False)
        emit(event="done", replaced=replaced, path=path)
    finally:
        app.DisplayAlerts = False
        app.Quit()


def save_as_dialog(work):
    """The common Save As dialog: the parent enters the file name natively and presses Save."""
    initial = os.path.join(work, "initial.xlsx")
    app, pid = _excel()
    try:
        _announce_soon(pid)
        name = app.GetSaveAsFilename(initial, "Excel Workbook (*.xlsx), *.xlsx")
        emit(event="done", filename=name if isinstance(name, str) else None)
    finally:
        app.DisplayAlerts = False
        app.Quit()


_HOME_TCODES = ("SESSION_MANAGER", "SMEN", "S000")


def _idle_home_session(deadline_s: float = 3.0):
    """An SAP GUI session that is idle and on its home screen, or None.

    Each session is probed in its own thread with a deadline: a frozen session
    hangs every call that touches it (even ``.Busy``), and must just be skipped.
    """
    import win32com.client

    sapgui = win32com.client.GetObject("SAPGUI")
    engine = sapgui.GetScriptingEngine
    candidates = []
    for ci in range(engine.Children.Count):
        conn = engine.Children(ci)
        for si in range(conn.Children.Count):
            candidates.append((ci, si))
    for ci, si in candidates:
        box = {}

        def probe():
            import pythoncom

            pythoncom.CoInitialize()
            try:
                s = win32com.client.GetObject("SAPGUI").GetScriptingEngine.Children(ci).Children(si)
                box["ok"] = (not s.Busy) and s.Info.Transaction in _HOME_TCODES
            except Exception:
                box["ok"] = False

        t = threading.Thread(target=probe, daemon=True)
        t.start()
        t.join(deadline_s)
        if box.get("ok"):
            return engine.Children(ci).Children(si)
    return None


def sap_export(work):
    """SE16N T001 (company codes: a tiny, read-only table) -> local unconverted file. Never opens Excel."""
    import win32process

    session = _idle_home_session()
    if session is None:
        emit(event="skip", reason="no idle SAP GUI session on its home screen")
        return
    hwnd = session.findById("wnd[0]").Handle
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    out = os.path.join(work, "t001.txt")
    emit(event="ready", pid=pid)  # SAP GUI's process: the parent watches it for security prompts
    try:
        session.findById("wnd[0]/tbar[0]/okcd").text = "/nSE16N"
        session.findById("wnd[0]").sendVKey(0)
        session.findById("wnd[0]/usr/ctxtGD-TAB").text = "T001"
        session.findById("wnd[0]").sendVKey(0)
        session.findById("wnd[0]/usr/txtGD-MAX_LINES").text = "50"
        session.findById("wnd[0]/tbar[1]/btn[8]").press()
        grid = None
        for _ in range(60):
            try:
                grid = session.findById("wnd[0]/usr/cntlRESULT_LIST/shellcont/shell")
                break
            except Exception:
                time.sleep(0.5)
        if grid is None:
            raise RuntimeError("SE16N T001 result grid did not load")
        rows = grid.RowCount
        grid.pressToolbarContextButton("&MB_EXPORT")  # Export menu of the ALV toolbar
        grid.selectContextMenuItem("&PC")  # local file, not &XXL: no Excel hand-off
        for _ in range(20):  # format popup (unconverted is the default) then the file dialog
            try:
                session.findById("wnd[1]/usr/ctxtDY_PATH")
                break
            except Exception:
                try:
                    session.findById("wnd[1]/tbar[0]/btn[0]").press()
                except Exception:
                    time.sleep(0.5)
        session.findById("wnd[1]/usr/ctxtDY_PATH").text = work
        session.findById("wnd[1]/usr/ctxtDY_FILENAME").text = "t001.txt"
        session.findById("wnd[1]/tbar[0]/btn[0]").press()  # may raise "SAP GUI Security": the parent answers it
        for _ in range(120):
            if os.path.exists(out) and os.path.getsize(out) > 0:
                break
            time.sleep(0.5)
        emit(event="done", path=out, grid_rows=rows,
             exists=os.path.exists(out), size=os.path.getsize(out) if os.path.exists(out) else 0)
    finally:
        try:  # back to the home screen, whatever happened
            for _ in range(3):
                try:
                    session.findById("wnd[1]").close()
                except Exception:
                    break
            session.findById("wnd[0]/tbar[0]/okcd").text = "/n"
            session.findById("wnd[0]").sendVKey(0)
        except Exception:
            pass


SCENARIOS = {"save_prompt": save_prompt, "overwrite_prompt": overwrite_prompt, "save_as_dialog": save_as_dialog,
             "sap_export": sap_export}

if __name__ == "__main__":
    import pythoncom

    pythoncom.CoInitialize()
    try:
        SCENARIOS[sys.argv[1]](sys.argv[2])
    except Exception as exc:  # reported to the parent, never swallowed
        import traceback

        frames = [f for f in traceback.extract_tb(exc.__traceback__) if f.filename == os.path.abspath(__file__)]
        where = (frames or traceback.extract_tb(exc.__traceback__))[-1]
        emit(event="error", error=f"{type(exc).__name__}: {exc} (at line {where.lineno}: {where.line})")
        time.sleep(0.2)
        sys.exit(1)
