"""locum.bridges.sap against a fake SAP GUI Scripting COM object graph (no SAP GUI needed)."""

from __future__ import annotations

import sys
from typing import Any, List

import pytest

from locum.bridges import sap
from locum.errors import (
    ActionTimeout,
    AmbiguousElement,
    BackendError,
    BackendUnavailable,
    ElementNotFound,
    FailSafeTriggered,
    NotSupported,
    PolicyViolation,
)
from locum.llm.safety import AuditLog, Policy


# ---------------------------------------------------------------------- fake COM graph
class FakeComError(Exception):
    """Shaped like pywintypes.com_error: (hresult, text, excepinfo, argerr)."""

    def __init__(self, description: str) -> None:
        super().__init__(-2147352567, "Exception occurred.", (0, "SAPGUI", description, None, 0, 0), None)


class Coll:
    def __init__(self, items: List[Any]) -> None:
        self.items = items

    @property
    def Count(self) -> int:
        return len(self.items)

    def __call__(self, i: int) -> Any:
        return self.items[i]

    def ElementAt(self, i: int) -> Any:
        return self.items[i]


class Comp:
    def __init__(self, type_: str, cid: str, **props: Any) -> None:
        object.__setattr__(self, "calls", [])
        self.Type = type_
        self.Id = cid
        self.Name = props.pop("Name", cid.rsplit("/", 1)[-1])
        self.Text = props.pop("Text", "")
        self.Tooltip = props.pop("Tooltip", "")
        self.Changeable = props.pop("Changeable", True)
        self.ContainerType = props.pop("ContainerType", False)
        self.kids: List[Comp] = props.pop("kids", [])
        self.Parent: Any = None
        for k in self.kids:
            k.Parent = self
        for k, v in props.items():
            setattr(self, k, v)

    @property
    def Children(self) -> Coll:
        return Coll(self.kids)

    def press(self) -> None:
        self.calls.append(("press",))

    def Select(self) -> None:
        self.calls.append(("Select",))

    def SetFocus(self) -> None:
        self.calls.append(("SetFocus",))

    def sendVKey(self, n: int) -> None:
        self.calls.append(("sendVKey", n))
        hook = getattr(self, "on_vkey", None)
        if hook:
            hook(n)

    def Close(self) -> None:
        self.calls.append(("Close",))

    def findById(self, rid: str) -> "Comp":
        for c in _walk(self):
            if c.Id.endswith("/" + rid):
                return c
        raise FakeComError("The control could not be found by id.")


def _walk(c: Comp):
    yield c
    for k in c.kids:
        yield from _walk(k)


class Table(Comp):
    """A GuiTableControl with 10 rows, 3 visible, one column 'Amount' (GetCell is visible-relative)."""

    def __init__(self, cid: str, session: "Session") -> None:
        super().__init__("GuiTableControl", cid, ContainerType=True)
        self.session = session
        self.RowCount = 10
        self.VisibleRowCount = 3
        self.data = {r: f"{r * 100}.00" for r in range(10)}
        self.VerticalScrollbar = type("SB", (), {})()
        self.VerticalScrollbar.Position = 0
        self.Columns = Coll([type("Col", (), {"Name": "WRBTR", "Title": "Amount"})()])

    def GetCell(self, r: int, c: int) -> Comp:
        if not (0 <= r < self.VisibleRowCount):
            raise FakeComError("row out of range")
        absolute = self.VerticalScrollbar.Position + r
        table = self

        class Cell(Comp):
            @property
            def Text(self) -> str:  # type: ignore[override]
                return table.data[absolute]

            @Text.setter
            def Text(self, v: str) -> None:
                table.data[absolute] = v

        cell = Cell.__new__(Cell)
        object.__setattr__(cell, "calls", [])
        cell.__dict__.update(Type="GuiTextField", Id=f"{self.Id}/txtWRBTR[{c},{r}]", Name="WRBTR", Tooltip="Amount",
                             Changeable=True, ContainerType=False, kids=[], Parent=self, MaxLength=13)
        return cell


class Grid(Comp):
    def __init__(self, cid: str) -> None:
        super().__init__("GuiShell", cid, SubType="GridView")
        self.RowCount = 4
        self.ColumnCount = 2
        self.ColumnOrder = Coll(["BUKRS", "BUTXT"])
        self.VisibleRowCount = 2
        self.FirstVisibleRow = 0
        self.loaded = {0, 1}
        self.values = {(r, "BUKRS"): f"{1000 + r}" for r in range(4)}
        self.values.update({(r, "BUTXT"): f"Company {r}" for r in range(4)})

    def __setattr__(self, name: str, value: Any) -> None:
        object.__setattr__(self, name, value)
        if name == "FirstVisibleRow" and hasattr(self, "loaded"):
            self.loaded.update(range(value, value + self.VisibleRowCount))

    def GetCellValue(self, r: int, c: str) -> str:
        return self.values[(r, c)] if r in self.loaded else ""

    def ModifyCell(self, r: int, c: str, v: str) -> None:
        self.calls.append(("ModifyCell", r, c, v))
        self.values[(r, c)] = v

    def PressToolbarButton(self, b: str) -> None:
        self.calls.append(("PressToolbarButton", b))


class Info:
    def __init__(self, system: str, client: str, user: str, tcode: str = "SESSION_MANAGER") -> None:
        self.SystemName, self.Client, self.User, self.Transaction = system, client, user, tcode
        self.Program, self.ScreenNumber, self.Language = "SAPMSYST", 100, "EN"


class Session(Comp):
    def __init__(self, cid: str, info: Info) -> None:
        self.Info = info
        self.Busy = False
        self.busy_polls = 0
        base = cid
        sbar = Comp("GuiStatusbar", f"{base}/wnd[0]/sbar", MessageType="", MessageId="", MessageNumber="")
        okcd = Comp("GuiOkCodeField", f"{base}/wnd[0]/tbar[0]/okcd", MaxLength=132)
        save = Comp("GuiButton", f"{base}/wnd[0]/tbar[0]/btn[11]", Tooltip="Save (Ctrl+S)")
        post = Comp("GuiButton", f"{base}/wnd[0]/tbar[1]/btn[8]", Text="Post")
        icon = Comp("GuiButton", f"{base}/wnd[0]/tbar[1]/btn[9]")
        lbl = Comp("GuiLabel", f"{base}/wnd[0]/usr/lblBUKRS", Text="Company Code")
        bukrs = Comp("GuiCTextField", f"{base}/wnd[0]/usr/ctxtBKPF-BUKRS", Text="", MaxLength=4, LeftLabel=lbl,
                     Tooltip="Company Code")
        pwd = Comp("GuiPasswordField", f"{base}/wnd[0]/usr/pwdRSYST-BCODE", MaxLength=40)
        ro = Comp("GuiTextField", f"{base}/wnd[0]/usr/txtRO", Text="locked", Changeable=False, MaxLength=10)
        combo = Comp("GuiComboBox", f"{base}/wnd[0]/usr/cmbLANG", Key="EN", Value="English",
                     Entries=Coll([type("E", (), {"Key": "EN", "Value": "English"})(),
                                   type("E", (), {"Key": "DE", "Value": "German"})()]))
        chk = Comp("GuiCheckBox", f"{base}/wnd[0]/usr/chkTEST", Text="Test run", Selected=False)
        radio = Comp("GuiRadioButton", f"{base}/wnd[0]/usr/radA", Text="Option A", Selected=False)
        usr = Comp("GuiUserArea", f"{base}/wnd[0]/usr", ContainerType=True,
                   kids=[lbl, bukrs, pwd, ro, combo, chk, radio])
        self.table = Table(f"{base}/wnd[0]/usr/tblSAPLTABLE", self)
        usr.kids.append(self.table)
        self.table.Parent = usr
        self.grid = Grid(f"{base}/wnd[0]/usr/cntlGRID/shellcont/shell")
        usr.kids.append(self.grid)
        self.grid.Parent = usr
        tbar0 = Comp("GuiToolbar", f"{base}/wnd[0]/tbar[0]", ContainerType=True, kids=[okcd, save])
        tbar1 = Comp("GuiToolbar", f"{base}/wnd[0]/tbar[1]", ContainerType=True, kids=[post, icon])
        self.wnd0 = Comp("GuiMainWindow", f"{base}/wnd[0]", Text="Enter G/L Account Document", Handle=4242,
                         ContainerType=True, kids=[tbar0, tbar1, usr, sbar])
        super().__init__("GuiSession", base, ContainerType=True, kids=[self.wnd0])
        self.wnd0.Parent = self
        self.ActiveWindow = self.wnd0
        self.sbar, self.okcd, self.save, self.post, self.icon = sbar, okcd, save, post, icon
        self.bukrs, self.pwd, self.ro, self.combo, self.chk, self.radio = bukrs, pwd, ro, combo, chk, radio
        self.started: List[str] = []

    def __getattribute__(self, name: str) -> Any:
        if name == "Busy":
            d = object.__getattribute__(self, "__dict__")
            if d.get("busy_polls", 0) > 0:
                d["busy_polls"] -= 1
                return True
        return object.__getattribute__(self, name)

    def StartTransaction(self, tcode: str) -> None:
        self.started.append(tcode)
        self.Info.Transaction = tcode


class Connection:
    def __init__(self, desc: str, sessions: List[Session], disabled: bool = False) -> None:
        self.Description = desc
        self._s = sessions
        self.DisabledByServer = disabled

    @property
    def Children(self) -> Coll:
        return Coll([] if self.DisabledByServer else self._s)


class Engine:
    def __init__(self, conns: List[Connection]) -> None:
        self.Children = Coll(conns)


def make_engine(*, two_sessions: bool = False, disabled: bool = False) -> Engine:
    s0 = Session("/app/con[0]/ses[0]", Info("PRD", "100", "ROBOT"))
    sessions = [s0]
    if two_sessions:
        sessions.append(Session("/app/con[0]/ses[1]", Info("PRD", "100", "ROBOT")))
    conns = [Connection("PRD - Production", sessions, disabled=disabled),
             Connection("QAS - Quality", [Session("/app/con[1]/ses[0]", Info("QAS", "200", "TESTER"))])]
    return Engine(conns)


@pytest.fixture
def engine(monkeypatch):
    eng = make_engine()
    monkeypatch.setattr(sap, "_engine_factory", lambda: eng)
    return eng


def _session(engine: Engine, **kw: Any) -> sap.SapSession:
    kw.setdefault("system", "PRD")
    gui = sap.SapGui(engine, sleep=lambda s: None, **{k: kw.pop(k) for k in list(kw) if k in ("policy", "confirm",
                                                                                                "audit", "desktop")})
    return gui.attach(**kw)


def raw(engine: Engine, ci: int = 0, si: int = 0) -> Session:
    return engine.Children(ci)._s[si]


# ---------------------------------------------------------------------- attach / sessions
def test_sessions_lists_every_session(engine):
    infos = sap.sessions()
    assert [(i.connection, i.session, i.system, i.client, i.user) for i in infos] == [
        (0, 0, "PRD", "100", "ROBOT"), (1, 0, "QAS", "200", "TESTER")]
    assert infos[0].description == "PRD - Production" and infos[0].to_dict()["busy"] is False


def test_attach_by_system_client_and_connection(engine):
    s = sap.attach(system="qas", client=200)
    assert s.info["system"] == "QAS" and s.info["client"] == "200"
    assert sap.attach(connection=0).info["system"] == "PRD"
    assert sap.attach(connection="QAS*").info["user"] == "TESTER"
    assert sap.attach(system="PRD - Production").info["system"] == "PRD"  # SAP Logon description


def test_attach_no_match_lists_sessions(engine):
    with pytest.raises(ElementNotFound) as ei:
        sap.attach(system="DEV")
    assert "PRD/100" in str(ei.value) and "QAS/200" in str(ei.value)


def test_attach_ambiguous_refuses(monkeypatch):
    eng = make_engine(two_sessions=True)
    monkeypatch.setattr(sap, "_engine_factory", lambda: eng)
    with pytest.raises(AmbiguousElement):
        sap.attach(system="PRD")
    assert sap.attach(system="PRD", session=1).com is raw(eng, 0, 1)


def test_attach_server_side_disabled_explains_user_scripting(monkeypatch):
    eng = Engine([Connection("PRD - Production", [], disabled=True)])
    monkeypatch.setattr(sap, "_engine_factory", lambda: eng)
    with pytest.raises(NotSupported) as ei:
        sap.attach(system="PRD")
    assert "sapgui/user_scripting" in str(ei.value)
    assert sap.sessions()[0].disabled_by_server is True


def test_attach_without_connections(monkeypatch):
    monkeypatch.setattr(sap, "_engine_factory", lambda: Engine([]))
    with pytest.raises(ElementNotFound, match="no open connection"):
        sap.attach()


def test_missing_pywin32_or_platform_raises_not_supported(monkeypatch):
    monkeypatch.setitem(sys.modules, "win32com", None)
    monkeypatch.setitem(sys.modules, "win32com.client", None)
    monkeypatch.setitem(sys.modules, "pythoncom", None)
    with pytest.raises(NotSupported) as ei:
        sap._load_engine()
    msg = str(ei.value)
    assert "locum[sap]" in msg or "only available on Windows" in msg


@pytest.mark.skipif(sys.platform != "win32", reason="the COM path is Windows-only")
def test_client_side_scripting_disabled_and_sap_not_running(monkeypatch):
    import types

    class SapGuiObj:
        GetScriptingEngine = None

    client = types.SimpleNamespace(GetObject=lambda name: SapGuiObj())
    win32com = types.ModuleType("win32com")
    win32com.client = client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "win32com", win32com)
    monkeypatch.setitem(sys.modules, "win32com.client", client)
    monkeypatch.setitem(sys.modules, "pythoncom", types.SimpleNamespace(CoInitialize=lambda: None))
    with pytest.raises(NotSupported, match="Enable scripting"):
        sap._load_engine()

    def no_sap(name):
        raise FakeComError("Operation unavailable")

    client.GetObject = no_sap
    with pytest.raises(BackendUnavailable, match="SAP Logon"):
        sap._load_engine()


# ---------------------------------------------------------------------- find / typed wrappers
def test_find_returns_typed_wrappers(engine):
    s = _session(engine)
    assert isinstance(s.find("wnd[0]/usr/ctxtBKPF-BUKRS"), sap.SapTextField)
    assert isinstance(s.find("wnd[0]/tbar[1]/btn[8]"), sap.SapButton)
    assert isinstance(s.find("wnd[0]/usr/cmbLANG"), sap.SapComboBox)
    assert isinstance(s.find("wnd[0]/usr/tblSAPLTABLE"), sap.SapTableControl)
    assert isinstance(s.find("wnd[0]/usr/cntlGRID/shellcont/shell"), sap.SapGridView)
    assert isinstance(s.find("wnd[0]"), sap.SapWindow)
    assert s.find("wnd[0]/usr/ctxtBKPF-BUKRS").id == "wnd[0]/usr/ctxtBKPF-BUKRS"
    assert s.exists("wnd[0]/sbar") and not s.exists("wnd[1]")
    with pytest.raises(ElementNotFound, match="could not be found by id"):
        s.find("wnd[1]/usr/txtX")


def test_find_with_timeout_waits_for_a_popup(engine):
    s = _session(engine)
    sess = raw(engine)
    polls = {"n": 0}
    real = sess.findById

    def late(rid):
        polls["n"] += 1
        if rid.startswith("wnd[1]") and polls["n"] < 3:
            raise FakeComError("The control could not be found by id.")
        return real("wnd[0]") if rid.startswith("wnd[1]") else real(rid)

    sess.findById = late  # type: ignore[assignment]
    assert s.find("wnd[1]", timeout=5).type == "GuiMainWindow"
    assert polls["n"] == 3


def test_text_field_set_and_read(engine):
    s = _session(engine)
    f = s.find("wnd[0]/usr/ctxtBKPF-BUKRS")
    f.text = "1000"
    assert raw(engine).bukrs.Text == "1000" and f.text == "1000"
    assert f.label == "Company Code"
    with pytest.raises(ValueError, match="MaxLength 4"):
        f.set_text("10000")
    assert raw(engine).bukrs.Text == "1000"  # nothing written
    with pytest.raises(ElementNotFound, match="not changeable"):
        s.find("wnd[0]/usr/txtRO").set_text("x")


def test_button_press_combo_checkbox_radio(engine):
    s = _session(engine)
    s.find("wnd[0]/tbar[1]/btn[8]").press()
    assert raw(engine).post.calls == [("press",)]
    c = s.find("wnd[0]/usr/cmbLANG")
    assert c.entries == [("EN", "English"), ("DE", "German")] and c.key == "EN" and c.value == "English"
    c.key = "DE"
    assert raw(engine).combo.Key == "DE"
    with pytest.raises(ValueError, match="not an entry"):
        c.set_key("FR")
    box = s.find("wnd[0]/usr/chkTEST")
    box.check()
    assert raw(engine).chk.Selected is True
    box.check()  # already checked: no write
    s.find("wnd[0]/usr/radA").select()
    assert raw(engine).radio.calls == [("Select",)]


def test_statusbar_and_check_status(engine):
    s = _session(engine)
    sb = raw(engine).sbar
    sb.Text, sb.MessageType, sb.MessageId, sb.MessageNumber = "Document 100000 was posted", "S", "F5", "312"
    assert s.check_status().message_type == "S"
    sb.Text, sb.MessageType = "Enter a company code", "E"
    with pytest.raises(sap.SapError) as ei:
        s.check_status()
    assert ei.value.message_type == "E" and "company code" in str(ei.value)
    sb.MessageType = "W"
    assert s.check_status().message_type == "W"
    with pytest.raises(sap.SapError):
        s.check_status(raise_on="EAW")
    assert s.statusbar.is_error is False


def test_send_vkey_and_start_transaction(engine):
    s = _session(engine)
    sb = s.send_vkey(0)
    assert raw(engine).wnd0.calls == [("sendVKey", 0)] and isinstance(sb, sap.SapStatusbar)
    s.start_transaction("/nFB50")
    assert raw(engine).started == ["FB50"] and s.transaction == "FB50"
    with pytest.raises(ValueError):
        s.start_transaction("  ")
    assert sap.vkey_combo(11) == "ctrl+s" and sap.vkey_combo(8) == "f8" and sap.vkey_combo(15) == "shift+f3"
    assert sap.vkey_combo(99) == "vkey99"


def test_start_transaction_falls_back_to_ok_code(engine):
    s = _session(engine)
    sess = raw(engine)

    def broken(tcode):
        raise FakeComError("not supported")

    sess.StartTransaction = broken  # type: ignore[assignment]
    s.start_transaction("SE16N")
    assert sess.okcd.Text == "/nSE16N" and sess.wnd0.calls == [("sendVKey", 0)]


def test_actions_wait_until_not_busy(engine):
    s = _session(engine)
    raw(engine).busy_polls = 3
    s.find("wnd[0]/tbar[1]/btn[8]").press()
    assert raw(engine).busy_polls == 0


def test_busy_session_times_out(engine):
    clock = {"t": 0.0}
    gui = sap.SapGui(engine, sleep=lambda d: clock.__setitem__("t", clock["t"] + d), clock=lambda: clock["t"],
                     default_timeout=1.0)
    s = gui.attach(system="PRD")
    raw(engine).busy_polls = 10 ** 6
    with pytest.raises(ActionTimeout, match="busy"):
        s.wait_ready()


def test_com_failure_becomes_backend_error(engine):
    s = _session(engine)

    def boom():
        raise FakeComError("The method got an invalid argument.")

    raw(engine).post.press = boom  # type: ignore[assignment]
    with pytest.raises(BackendError, match="invalid argument"):
        s.find("wnd[0]/tbar[1]/btn[8]").press()


def test_table_control_absolute_rows_scroll(engine):
    s = _session(engine)
    t = s.find("wnd[0]/usr/tblSAPLTABLE")
    assert t.row_count == 10 and t.columns == [("WRBTR", "Amount")]
    assert t.read(1, "Amount") == "100.00"
    assert t.read(7, 0) == "700.00"  # scrolled
    assert raw(engine).table.VerticalScrollbar.Position == 7
    t.write(8, "WRBTR", "5.00")
    assert raw(engine).table.data[8] == "5.00"
    with pytest.raises(IndexError):
        t.read(10, 0)
    with pytest.raises(ElementNotFound, match="Column"):
        t.read(0, "Nope")


def test_grid_view_rows_load_lazily_and_write(engine):
    s = _session(engine)
    g = s.find("wnd[0]/usr/cntlGRID/shellcont/shell")
    assert g.columns == ["BUKRS", "BUTXT"]
    rows = g.rows()
    assert rows[3] == {"BUKRS": "1003", "BUTXT": "Company 3"}  # row 3 was not loaded until scrolled to
    g.write(2, "BUTXT", "New name")
    g.press_toolbar_button("&MB_EXPORT")
    assert ("ModifyCell", 2, "BUTXT", "New name") in raw(engine).grid.calls
    assert ("PressToolbarButton", "&MB_EXPORT") in raw(engine).grid.calls


def test_selectors_over_sap_nodes(engine):
    s = _session(engine)
    found = s.find_all('button[name*="Save"]')
    assert [c.id for c in found] == ["wnd[0]/tbar[0]/btn[11]"]
    assert [c.id for c in s.find_all('edit[name="Company Code"]')] == ["wnd[0]/usr/ctxtBKPF-BUKRS"]
    assert [c.type for c in s.find_all("edit") if "password" in sap.SapNode(c.com).states] == ["GuiPasswordField"]
    assert "Company Code" in s.tree()
    node = sap.SapNode(raw(engine).ro, s)
    assert "readonly" in node.states and node.role == "edit"
    assert sap.SapNode(raw(engine).grid, s).role == "table"


# ---------------------------------------------------------------------- policy
def test_policy_confirms_post_and_ctrl_s(engine):
    asked: List[str] = []
    audit = AuditLog()
    s = _session(engine, policy=Policy(), confirm=lambda d, det: asked.append(d) or False, audit=audit)
    with pytest.raises(PolicyViolation, match="declined"):
        s.find("wnd[0]/tbar[1]/btn[8]").press()  # "Post" matches the default confirm patterns
    assert raw(engine).post.calls == []
    with pytest.raises(PolicyViolation):
        s.send_vkey(11)  # ctrl+s is a confirm key
    assert raw(engine).wnd0.calls == []
    s.send_vkey(0)  # plain Enter: no confirmation
    assert raw(engine).wnd0.calls == [("sendVKey", 0)]
    statuses = [e["status"] for e in audit.read()]
    assert statuses == ["blocked", "blocked", "allowed"]
    assert all(e["bridge"] == "sap" for e in audit.read())


def test_policy_icon_button_without_label_needs_confirmation(engine):
    s = _session(engine, policy=Policy())
    with pytest.raises(PolicyViolation, match="cannot be identified"):
        s.find("wnd[0]/tbar[1]/btn[9]").press()


def test_policy_allow_list_with_selector_and_window_rules(engine):
    pol = Policy(allow=['edit[name="Company Code"]', {"window": "Enter G/L*", "tools": ["sap_start_transaction"]}],
                 confirm_patterns=())
    s = _session(engine, policy=pol)
    s.find("wnd[0]/usr/ctxtBKPF-BUKRS").set_text("1000")
    s.start_transaction("FB50")
    with pytest.raises(PolicyViolation, match="no allow rule"):
        s.find("wnd[0]/usr/cmbLANG").set_key("DE")
    assert raw(engine).combo.Key == "EN"


def test_policy_dry_run_skips_and_password_is_masked(engine):
    audit = AuditLog()
    s = _session(engine, policy=Policy(dry_run=True), audit=audit)
    s.find("wnd[0]/usr/pwdRSYST-BCODE").set_text("hunter2")
    assert getattr(raw(engine).pwd, "Text") == ""
    ev = audit.read()[-1]
    assert ev["status"] == "dry_run" and ev["args"]["text"] == "***"


def test_policy_blocked_app_and_budget(engine):
    s = _session(engine, policy=Policy(blocked_apps=["saplogon"]))
    with pytest.raises(PolicyViolation, match="blocked"):
        s.send_vkey(0)
    s2 = _session(engine, policy=Policy.permissive(max_actions=1))
    s2.send_vkey(0)
    with pytest.raises(PolicyViolation, match="budget"):
        s2.send_vkey(0)


def test_guarded_desktop_lends_its_policy(engine, mock_backend):
    from locum import Desktop
    from locum.llm.safety import GuardedDesktop

    gd = GuardedDesktop(Desktop(mock_backend, failsafe=False), Policy(), confirm=lambda d, det: False)
    s = _session(engine, desktop=gd)
    assert s.gate.policy is gd.policy and s.gate.audit is gd.audit
    with pytest.raises(PolicyViolation):
        s.find("wnd[0]/tbar[1]/btn[8]").press()


def test_failsafe_abort_stops_actions(engine):
    from locum import failsafe

    s = _session(engine)
    failsafe.abort("test")
    try:
        with pytest.raises(FailSafeTriggered):
            s.find("wnd[0]/tbar[1]/btn[8]").press()
    finally:
        failsafe.reset_abort()
    assert raw(engine).post.calls == []
