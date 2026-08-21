"""
_stub.py — Generic, project-agnostic helper to import a codebase's modules WITHOUT
installing its heavy optional dependencies.

Many research codebases do top-level `import <heavy_dep>` (timm, wilds, detectron2, ...)
inside a module you want to introspect, even when the specific class you need does not
use that dep. Rather than install packages (often disallowed by a research goal), you can
register lightweight placeholder modules in sys.modules so the import line succeeds.

This helper is NOT tied to any project. You tell it which modules to stub via a spec
string; it never guesses.

Spec format (comma-separated entries):
    "timm"                                   -> empty stub module `timm`
    "wilds.datasets.foo:BarDataset"          -> stub module with attribute `BarDataset`
    "pkg.sub:ClassA:ClassB"                  -> stub module with attributes ClassA, ClassB

Parent packages are created automatically (e.g. stubbing `a.b.c` also creates `a`, `a.b`).
A real, importable module of the same name is always preferred and left untouched.
"""
import sys
import types


class _StubUnavailable:
    """Placeholder for a stubbed class/attribute. Importing it is fine; using it raises,
    so you find out immediately if the non-stub code path was hit by mistake."""
    def __init__(self, *args, **kwargs):
        raise RuntimeError(
            "This symbol was stubbed out (its real dependency is not installed). "
            "The code path you're on was expected not to need it."
        )


def _ensure_module(name):
    if name in sys.modules:
        return sys.modules[name]
    mod = types.ModuleType(name)
    mod.__stub__ = True
    mod.__path__ = []  # treat as a package so submodules can be attached
    sys.modules[name] = mod
    # link into parent
    if "." in name:
        parent, child = name.rsplit(".", 1)
        pmod = _ensure_module(parent)
        setattr(pmod, child, mod)
    return mod


def install_stubs(spec):
    """Install stub modules from a spec string. Returns the list of module names stubbed
    (only those that were actually missing — real ones are left alone)."""
    if not spec:
        return []
    stubbed = []
    for entry in spec.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        mod_name = parts[0].strip()
        attrs = [p.strip() for p in parts[1:] if p.strip()]
        # prefer a real module
        try:
            __import__(mod_name)
            real = True
        except Exception:  # noqa: BLE001
            real = False
        if real:
            mod = sys.modules[mod_name]
        else:
            mod = _ensure_module(mod_name)
            stubbed.append(mod_name)
        for a in attrs:
            if not hasattr(mod, a):
                setattr(mod, a, _StubUnavailable)
    return stubbed
