"""The safe-by-default policy plumbing shared by :class:`~locum.rpa.Robot`, flows and the CLI.

Kept free of heavy imports (``import locum`` must stay light): :mod:`locum.llm.safety`
is only imported when a default or preset policy is actually resolved.
"""

from __future__ import annotations

from typing import Any, Optional

__all__ = ["DEFAULT_POLICY", "POLICY_OFF", "POLICY_PRESETS", "resolve_policy", "policy_label"]


class _DefaultPolicy:
    """Sentinel default of ``policy=``: "apply :meth:`Policy.default`"."""

    _instance: Optional["_DefaultPolicy"] = None

    def __new__(cls) -> "_DefaultPolicy":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "DEFAULT_POLICY"

    def __reduce__(self) -> str:
        return "DEFAULT_POLICY"


class _PolicyOff:
    """The explicit opt-out (``Policy.off()``): actions are not gated at all."""

    _instance: Optional["_PolicyOff"] = None

    def __new__(cls) -> "_PolicyOff":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "Policy.off()"

    def __reduce__(self) -> str:
        return "POLICY_OFF"


#: Default value of ``Robot(policy=...)`` / ``run_flow(..., policy=...)``: apply :meth:`Policy.default`.
DEFAULT_POLICY = _DefaultPolicy()
#: The explicit "no policy" value returned by :meth:`Policy.off` (``None`` and ``False`` mean the same).
POLICY_OFF = _PolicyOff()
#: Preset names accepted wherever a policy is (``policy="strict"``, ``locum run --policy strict``).
POLICY_PRESETS = ("default", "strict", "permissive", "off")


def resolve_policy(policy: Any) -> Any:
    """The :class:`~locum.llm.safety.Policy` (or ``None`` = ungated) a ``policy=`` argument stands for.

    * :data:`DEFAULT_POLICY` (the default) or ``True`` -> a new :meth:`Policy.default`;
    * ``None``, ``False``, :meth:`Policy.off` or ``"off"`` -> ``None`` (explicit opt-out);
    * ``"default"`` / ``"strict"`` / ``"permissive"`` -> that preset;
    * a :class:`Policy` -> itself.
    """
    if policy is None or policy is False or policy is POLICY_OFF:
        return None
    if policy is DEFAULT_POLICY or policy is True:
        from .llm.safety import Policy

        return Policy.default()
    if isinstance(policy, str):
        name = policy.strip().lower()
        if name in ("off", "none"):
            return None
        from .llm.safety import Policy

        if name == "default":
            return Policy.default()
        if name == "strict":
            return Policy.strict()
        if name == "permissive":
            return Policy.permissive()
        raise ValueError(f"Unknown policy preset {policy!r}; expected one of {', '.join(POLICY_PRESETS)}")
    from .llm.safety import Policy

    if not isinstance(policy, Policy):
        raise TypeError(f"policy must be a Policy, a preset name ({', '.join(POLICY_PRESETS)}), "
                        f"None or Policy.off(), got {type(policy).__name__}")
    return policy


def policy_label(policy: Any) -> str:
    """Short name of a resolved policy for reports: ``"off"``, the preset name, or ``"custom"``."""
    if policy is None:
        return "off"
    return str(getattr(policy, "preset", None) or "custom")
