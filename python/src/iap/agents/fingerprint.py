"""Code fingerprint of an alpha (G1): what a pre-registration commits to.

``fingerprint(alpha_id)`` returns ``{"code_hash", "features", "feature_hash"}``:

- ``code_hash`` - sha256 of the source of every class in the alpha's MRO that
  lives in ``iap.alpha`` (the alpha class and its bases), line endings
  normalised.  Module-level helpers outside those classes are not covered.
- ``features`` - the registry feature names the alpha declares it reads,
  closed over ``depends_on``.
- ``feature_hash`` - sha256 of those features' registry entries plus the
  source of the feature family modules that compute them.

An id that is not a flagship alpha (a combination id, a maker-study id)
has no fingerprint (``None``): its pre-registration carries none and the
gate cannot check one, which it says.
"""

from __future__ import annotations

import hashlib
import inspect
from typing import Any

from iap.agents.blackboard import canonical


def _src(obj: Any) -> str:
    return inspect.getsource(obj).replace("\r\n", "\n")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fingerprint(alpha_id: str) -> dict[str, Any] | None:
    from iap import alpha
    from iap.features import registry

    if alpha_id not in alpha.ALPHA_CLASSES:
        return None
    cls = alpha.ALPHA_CLASSES[alpha_id]
    classes = [c for c in cls.__mro__ if c.__module__.startswith("iap.alpha")]
    code = "\n".join(f"# {c.__module__}.{c.__qualname__}\n{_src(c)}" for c in classes)
    specs = {s.name: s for s in registry.build_registry()}
    todo, used = list(cls.features), set()
    while todo:
        name = todo.pop()
        if name in used or name not in specs:
            continue
        used.add(name)
        todo.extend(specs[name].depends_on)
    names = sorted(used)
    families = sorted({specs[n].family for n in names})
    feat = canonical(
        {
            "specs": [specs[n].to_dict() for n in names],
            "families": {f: _sha(_src(registry.family_module(f))) for f in families},
        }
    )
    return {"code_hash": _sha(code), "features": names, "feature_hash": _sha(feat)}
