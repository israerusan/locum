"""The safety hook shared by the bridges: policy check, dry-run, audit, fail-safe.

Bridge actions (an SAP ``press()``, a Java ``doAccessibleActions``) send no
mouse or keyboard input, so neither :class:`~locum.llm.safety.GuardedDesktop`
nor the fail-safe in :class:`~locum.mouse.Mouse` ever sees them. Every acting
bridge call therefore goes through :meth:`ActionGate.allow` first, which does
what ``GuardedDesktop._gate`` does for desktop actions:

* :meth:`Policy.check <locum.llm.safety.Policy.check>` with the same context
  keys (``target``, ``target_node``, ``target_app``, ``target_window``,
  ``keys``, ``selector``, ``description``...), so confirm patterns, allow
  rules, ``allowed_apps``/``blocked_apps``, budgets and rate limits apply;
* ``dry_run``: the action is skipped and audited as ``dry_run``;
* one ``rpa_action`` event per decision in the :class:`AuditLog` (typed text
  for password fields is ``***``);
* the fail-safe abort flag (``locum.failsafe.abort()``, the kill hotkey) and
  the running call's cancel scope are honoured before the action.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Tuple

from .._cancel import check as _cancel_check


class ActionGate:
    """Policy gate for bridge actions (``policy=None``: everything runs, nothing is audited).

    Args:
        policy: A :class:`~locum.llm.safety.Policy`, or ``None``.
        confirm: Confirmation callback overriding the policy's own ``confirm``.
        audit: :class:`~locum.llm.safety.AuditLog` receiving the decisions
            (default with a policy: a new in-memory log).
        failsafe: Object with a ``check()`` method (a desktop's
            :class:`~locum.failsafe.FailSafe`); default: the global abort flag only.
        bridge: Bridge name recorded in audit events (``"sap"``, ``"java"``).
    """

    def __init__(self, policy: Any = None, *, confirm: Optional[Callable[[str, Dict[str, Any]], bool]] = None,
                 audit: Any = None, failsafe: Any = None, bridge: str = "") -> None:
        if policy is not None:
            from ..llm.safety import AuditLog, Policy  # noqa: PLC0415

            if not isinstance(policy, Policy):
                raise TypeError(f"policy must be a locum.llm.safety.Policy, got {type(policy).__name__}")
            if audit is None:
                audit = AuditLog()
        self.policy = policy
        self.confirm = confirm
        self.audit = audit
        self.failsafe = failsafe
        self.bridge = bridge

    @classmethod
    def resolve(cls, desktop: Any, policy: Any = None, confirm: Any = None, audit: Any = None, *,
                bridge: str = "") -> Tuple[Any, "ActionGate"]:
        """``(raw desktop, gate)``: a :class:`~locum.llm.safety.GuardedDesktop` lends its policy,
        confirm callback and audit log unless they are given explicitly."""
        raw = desktop
        unwrapped = getattr(type(desktop), "unwrapped", None)
        if desktop is not None and isinstance(unwrapped, property):
            raw = desktop.unwrapped
            if policy is None:
                policy = desktop.policy
                confirm = confirm if confirm is not None else desktop.confirm
                audit = audit if audit is not None else desktop.audit
        failsafe = getattr(raw, "failsafe", None) if raw is not None else None
        return raw, cls(policy, confirm=confirm, audit=audit, failsafe=failsafe, bridge=bridge)

    # ------------------------------------------------------------------ checks
    def guard(self) -> None:
        """Raise :class:`~locum.errors.FailSafeTriggered` after an abort (and honour cancel scopes)."""
        _cancel_check()
        check = getattr(self.failsafe, "check", None)
        if callable(check):
            check()
            return
        from .. import failsafe as _fs  # noqa: PLC0415
        from ..errors import FailSafeTriggered  # noqa: PLC0415

        if _fs.is_aborted():
            reason = _fs.abort_reason()
            raise FailSafeTriggered("Automation aborted" + (f": {reason}" if reason else ""))

    def _record(self, tool: str, args: Dict[str, Any], status: str, description: str, ctx: Dict[str, Any],
                error: Optional[str] = None) -> None:
        if self.audit is None:
            return
        event: Dict[str, Any] = {"event": "rpa_action", "bridge": self.bridge, "tool": tool, "args": args,
                                 "status": status, "description": description}
        for key in ("allowed_by", "needs_confirmation"):
            if ctx.get(key):
                event[key] = ctx[key]
        if error is not None:
            event["error"] = error
        try:
            self.audit.record(event)
        except Exception:  # noqa: BLE001 - an audit failure must not turn into an action failure
            pass

    def allow(self, tool: str, args: Dict[str, Any], ctx: Dict[str, Any], description: str, *,
              secret: bool = False) -> bool:
        """Check the fail-safe and the policy before an action.

        Returns ``True`` when the action may run, ``False`` in dry-run mode (skip
        it). Raises :class:`~locum.errors.PolicyViolation` when it is refused and
        :class:`~locum.errors.FailSafeTriggered` after an abort.
        """
        self.guard()
        if self.policy is None:
            return True
        from ..errors import PolicyViolation  # noqa: PLC0415

        ctx.setdefault("description", description)
        clean = {k: v for k, v in args.items() if v is not None}
        logged = {k: ("***" if secret and isinstance(v, str) else v) for k, v in clean.items()}
        if secret:
            from ..llm.safety import _add_sensitive  # noqa: PLC0415

            _add_sensitive(ctx, clean)
        try:
            self.policy.check(tool, clean, context=ctx, confirm=self.confirm)
        except PolicyViolation as exc:
            self._record(tool, logged, "blocked", description, ctx, error=str(exc))
            raise
        if self.policy.dry_run and not self.policy.is_read_only(tool):
            self._record(tool, logged, "dry_run", description, ctx)
            return False
        self._record(tool, logged, "allowed", description, ctx)
        return True


__all__ = ["ActionGate"]
