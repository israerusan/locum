"""The safety policy is on by default for robots, flows and ``locum run`` (0.3 breaking change)."""

from __future__ import annotations

import io
import json
import pickle

import pytest

from locum.backends.mock import MockBackend
from locum.cli import EXIT_FAILURE, EXIT_OK, EXIT_USAGE, main
from locum.desktop import Desktop
from locum.errors import PolicyViolation
from locum.llm import DesktopToolkit
from locum.llm.safety import (
    CONFIRM_KEYWORDS,
    DEFAULT_CONFIRM,
    DEFAULT_POLICY,
    POLICY_OFF,
    STRICT_KEYWORDS,
    AllowRule,
    AuditLog,
    GuardedDesktop,
    Policy,
    fold_text,
)
from locum.rpa import Robot, run_flow, validate_flow


@pytest.fixture
def app():
    be = MockBackend(width=1280, height=800)
    win = be.add_window("Invoice 42", (100, 100, 500, 400), app="erp")
    w = {
        "ok": win.add_button("OK", (20, 340, 80, 30)),
        "post": win.add_button("Post", (120, 340, 80, 30)),
        "delete": win.add_button("Supprimer la ligne", (220, 340, 120, 30)),
        "amount": win.add_edit("Amount", (20, 90, 250, 28), automation_id="amount"),
    }
    d = Desktop(be, failsafe=False, sleep=lambda s: None, default_timeout=0)
    return be, d, w


def cli(argv, desktop):
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), desktop=desktop, stdout=out, stderr=err, stdin=io.StringIO(""))
    return code, out.getvalue(), err.getvalue()


def write_flow(tmp_path, text):
    path = tmp_path / "flow.json"
    path.write_text(json.dumps(text), encoding="utf-8")
    return str(path)


# ----------------------------------------------------------------------- default applied


def test_robot_applies_the_default_policy(app, tmp_path):
    be, d, w = app
    r = Robot(d, run_dir=tmp_path, save_reports=False, screenshot_on_error=False)
    assert isinstance(r.policy, Policy) and r.policy.preset == "default"
    assert isinstance(r.desktop, GuardedDesktop) and r.desktop.unwrapped is d
    assert isinstance(r.audit, AuditLog)
    r.desktop.find("edit#amount").set_text("12.50")  # benign steps run
    r.desktop.click('button "OK"')
    assert w["amount"].value == "12.50" and w["ok"].clicks == 1
    with pytest.raises(PolicyViolation):
        r.desktop.click('button "Post"')
    assert w["post"].clicks == 0
    report = r.start()
    assert report.metadata["policy"] == "default" and report.metadata["policy_mode"] == "heuristic"


def test_run_flow_applies_the_default_policy_and_records_it(app):
    be, d, w = app
    report = run_flow({"steps": [{"click": 'button "OK"'}, {"click": 'button "Post"'}]}, d)
    assert not report.ok and "PolicyViolation" in report.error
    assert w["ok"].clicks == 1 and w["post"].clicks == 0
    assert report.metadata["policy"] == "default" and report.metadata["policy_mode"] == "heuristic"


def test_default_sentinel_and_preset_names(app):
    be, d, w = app
    assert repr(DEFAULT_POLICY) == "DEFAULT_POLICY" and repr(POLICY_OFF) == "Policy.off()"
    assert Policy.off() is POLICY_OFF and not POLICY_OFF
    assert pickle.loads(pickle.dumps(DEFAULT_POLICY)) is DEFAULT_POLICY
    for name in ("default", "strict", "permissive"):
        assert Robot(d, policy=name, save_reports=False).policy.preset == name
    assert Robot(d, policy="off", save_reports=False).policy is None
    with pytest.raises(ValueError):
        Robot(d, policy="lax")
    with pytest.raises(TypeError):
        Robot(d, policy=object())


def test_llm_entry_points_share_the_default_preset(app):
    be, d, w = app
    assert DesktopToolkit(d).policy.preset == "default"
    assert Policy().preset is None and Policy.default().confirm_patterns  # Policy() is still the same rules


# --------------------------------------------------------------------- explicit opt-out


@pytest.mark.parametrize("off", [None, False, Policy.off(), "off"])
def test_explicit_opt_out_is_ungated(app, tmp_path, off):
    be, d, w = app
    r = Robot(d, run_dir=tmp_path, save_reports=False, policy=off)
    assert r.policy is None and r.desktop is d
    r.desktop.click('button "Post"')
    report = run_flow({"steps": [{"click": 'button "Post"'}]}, d, policy=off)
    assert report.ok and w["post"].clicks == 2
    assert report.metadata["policy"] == "off"


# ------------------------------------------------------------- unattended confirm -> block


def test_unattended_confirmation_is_blocked_with_a_how_to_allow_message(app, tmp_path):
    be, d, w = app
    r = Robot(d, run_dir=tmp_path, save_reports=False)
    with pytest.raises(PolicyViolation) as exc:
        r.desktop.click('button "Post"')
    msg = str(exc.value)
    assert "needs human confirmation" in msg and "Nobody is there to confirm" in msg
    assert "Policy.default(exempt=['button \"Post\"'])" in msg  # a ready-made rule for this target
    assert "confirm=" in msg and "Policy.off()" in msg and "--no-policy" in msg
    assert exc.value.needs_confirmation
    assert r.audit.events[-1]["status"] == "blocked" and "Nobody is there" in r.audit.events[-1]["error"]


def test_a_confirm_callback_turns_the_block_into_a_question(app, tmp_path):
    be, d, w = app
    asked = []
    r = Robot(d, run_dir=tmp_path, save_reports=False, confirm=lambda desc, details: asked.append(desc) or True)
    r.desktop.click('button "Post"')
    assert w["post"].clicks == 1 and asked == ["click Post"]


def test_shortcut_is_blocked_unattended_and_exempt_rule_lets_it_through(app):
    be, d, w = app
    d.find("edit#amount").click()
    with pytest.raises(PolicyViolation, match="ctrl\\+s"):
        GuardedDesktop(d, Policy.default()).hotkey("ctrl", "s")
    g = GuardedDesktop(d, Policy.default(exempt=[{"window": "Invoice*", "keys": "mod+s"}]))
    g.hotkey("ctrl", "s")
    assert g.audit.events[-1]["status"] == "allowed"
    assert g.audit.events[-1]["exempted_by"] == "AllowRule(window='Invoice*', keys='mod+s')"
    with pytest.raises(PolicyViolation):  # the exemption is for that window's shortcut, not every commit
        g.click('button "Post"')


def test_exempt_rule_for_a_selector(app):
    be, d, w = app
    g = GuardedDesktop(d, Policy.default(exempt=['button "Post"']))
    g.click('button "Post"')
    assert w["post"].clicks == 1
    with pytest.raises(PolicyViolation):
        g.click('button "Supprimer la ligne"')


def test_exempt_never_authorizes_in_allow_list_mode_nor_skips_require_confirmation(app):
    be, d, w = app
    g = GuardedDesktop(d, Policy(allow=[], exempt=['button "Post"']))
    with pytest.raises(PolicyViolation, match="no allow rule"):
        g.click('button "Post"')
    p = Policy.default(exempt=["tool=erp_post"])
    with pytest.raises(PolicyViolation):
        p.check("erp_post", {}, context={"integration_tool": True, "require_confirmation": True})


def test_flow_document_exempt_rules(app):
    be, d, w = app
    flow = {"exempt": ['button "Post"'], "steps": [{"click": 'button "Post"'}]}
    report = run_flow(flow, d)
    assert report.ok and w["post"].clicks == 1
    assert report.metadata["policy_exempt"] == ["AllowRule(selector='button \"Post\"')"]
    # Too broad, or in a sub-flow: rejected before anything runs.
    assert any("too broad" in p for p in validate_flow({"exempt": ["window=*"], "steps": []}))
    sub = {"steps": [], "flows": {"inner": {"exempt": ['button "Post"'], "steps": []}}}
    assert any("only allowed in the top-level flow" in p for p in validate_flow(sub))
    # A document's exemption does not leak into the caller's policy object.
    policy = Policy.default()
    run_flow(flow, d, policy=policy)
    assert policy.exempt == ()
    with pytest.raises(PolicyViolation):
        GuardedDesktop(d, policy).click('button "Post"')


def test_flow_exempt_does_not_bypass_an_allow_list(app):
    be, d, w = app
    flow = {"exempt": ['button "Post"'], "steps": [{"click": 'button "Post"'}]}
    report = run_flow(flow, d, policy=Policy.strict(allow=['button "OK"']))
    assert not report.ok and w["post"].clicks == 0
    assert report.metadata["policy"] == "strict" and report.metadata["policy_mode"] == "allow-list"


# ------------------------------------------------------------------------------- CLI flags


def test_cli_run_applies_the_default_policy(app, tmp_path):
    be, d, w = app
    flow = write_flow(tmp_path, {"steps": [{"click": 'button "Post"'}]})
    code, out, err = cli(["run", flow], d)
    assert code == EXIT_FAILURE and w["post"].clicks == 0
    assert "--no-policy" in err


def test_cli_run_no_policy_and_policy_off(app, tmp_path):
    be, d, w = app
    flow = write_flow(tmp_path, {"steps": [{"click": 'button "Post"'}]})
    code, out, _ = cli(["--json", "run", flow, "--no-policy"], d)
    assert code == EXIT_OK and json.loads(out)["metadata"]["policy"] == "off"
    code, out, _ = cli(["run", flow, "--policy", "off"], d)
    assert code == EXIT_OK and w["post"].clicks == 2


def test_cli_run_exempt_strict_and_usage_errors(app, tmp_path):
    be, d, w = app
    flow = write_flow(tmp_path, {"steps": [{"click": 'button "Post"'}]})
    code, out, _ = cli(["--json", "run", flow, "--exempt", 'button "Post"'], d)
    data = json.loads(out)
    assert code == EXIT_OK and w["post"].clicks == 1 and data["metadata"]["policy"] == "default"
    code, out, _ = cli(["--json", "run", flow, "--policy", "strict"], d)
    assert code == EXIT_FAILURE and json.loads(out)["metadata"]["policy"] == "strict"
    assert cli(["run", flow, "--no-policy", "--exempt", "x"], d)[0] == EXIT_USAGE
    assert cli(["run", flow, "--confirm"], d)[0] == EXIT_USAGE  # stdin is not a terminal
    assert cli(["run", flow, "--exempt", "window="], d)[0] == EXIT_USAGE


def test_cli_run_dry_run_and_audit(app, tmp_path):
    be, d, w = app
    flow = write_flow(tmp_path, {"steps": [{"click": 'button "OK"'}]})
    audit = tmp_path / "audit.jsonl"
    code, _, _ = cli(["run", flow, "--dry-run", "--audit", str(audit)], d)
    assert code == EXIT_OK and w["ok"].clicks == 0
    assert json.loads(audit.read_text(encoding="utf-8").splitlines()[0])["status"] == "dry_run"


# ------------------------------------------------------------------- multilingual keywords


@pytest.mark.parametrize("label", [
    # English / German (as before)
    "Delete", "Pay now", "Submit", "Buchen", "Überweisen", "Löschen",
    # German without umlauts, and other inflections
    "Uberweisen", "Ueberweisen", "Loeschen", "Absenden", "Bezahlen",
    # French
    "Supprimer", "Supprimer la ligne", "Payer", "Paiement", "Virement", "Envoyer", "Valider", "Règlement",
    "Reglement", "Transférer", "Comptabiliser", "Rembourser",
    # Spanish
    "Eliminar", "Borrar", "Pagar", "Transferir", "Transferencia", "Enviar", "Envío", "Comprar", "Aprobar",
    "Contabilizar", "Realizar pedido",
    # Portuguese
    "Excluir", "Apagar", "Pagamento", "Transferência", "Lançar", "Lancar", "Finalizar compra", "Aprovar",
    # Italian
    "Elimina", "Cancella", "Paga", "Pagamento", "Bonifico", "Invia", "Inoltra", "Acquista", "Approva",
    # Dutch
    "Verwijderen", "Verwijder", "Betalen", "Betaling", "Overboeken", "Verzenden", "Versturen", "Afrekenen",
    "Goedkeuren", "Bestellen",
])
def test_multilingual_destructive_labels_need_confirmation(label):
    reason = Policy.default()._tripwire_reason("click", {}, {"target": label})
    assert reason is not None, label


@pytest.mark.parametrize("label", [
    "OK", "Cancel", "Next", "Settings", "Paste", "Page 2", "Calculate", "Save", "Open", "Search", "Rechercher",
    "Buscar", "Pesquisar", "Cerca", "Zoeken", "Suchen", "Annuler", "Weiter", "Suivant", "Siguiente", "Avanti",
    "Volgende", "Name Box", "Region", "Units", "Revenue", "Vendor", "Amount", "Due date", "Clear",
])
def test_benign_labels_pass_the_default_tripwire(label):
    assert Policy.default()._tripwire_reason("click", {}, {"target": label}) is None, label


@pytest.mark.parametrize("label", ["Enregistrer", "Guardar", "Salvar", "Salva", "Opslaan", "Speichern",
                                   "Confirmer", "Bestätigen", "Formater", "Effacer", "Formatear", "Svuota"])
def test_strict_adds_the_multilingual_extras(label):
    assert Policy.default()._tripwire_reason("click", {}, {"target": label}) is None
    assert Policy.strict()._tripwire_reason("click", {}, {"target": label}) is not None


def test_keyword_tables_and_accent_folding():
    assert set(CONFIRM_KEYWORDS) == {"en", "de", "fr", "es", "pt", "it", "nl"} == set(STRICT_KEYWORDS)
    assert len(DEFAULT_CONFIRM) == sum(len(v) for v in CONFIRM_KEYWORDS.values())
    assert fold_text("Überweisung, Règlement, Transferência, Straße") == "Uberweisung, Reglement, Transferencia, Strasse"
    # Accented user patterns still match plain text, and plain patterns accented text.
    p = Policy(confirm_patterns=[r"\bréglér\b"])
    assert p._tripwire_reason("click", {}, {"target": "Regler"}) is not None
    p = Policy(confirm_patterns=[r"\bloschen\b"])
    assert p._tripwire_reason("click", {}, {"target": "Löschen"}) is not None
    # Precompiled patterns are matched against the original text too.
    import re
    p = Policy(confirm_patterns=[re.compile("Ü")])
    assert p._tripwire_reason("click", {}, {"target": "Über"}) is not None


def test_allow_rule_coerce_used_by_exempt():
    assert Policy(exempt='button "X"').exempt == (AllowRule(selector='button "X"'),)
