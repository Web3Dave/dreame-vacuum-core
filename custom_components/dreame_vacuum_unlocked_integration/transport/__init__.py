"""Network transport for Dreame's cloud API.

Vendored, not rewritten. `protocol.py` (login, request signing, MQTT) and
`signing.py` (the MD5+salt scheme) are proven working code carried over from
the companion add-on rather than re-derived: the signing algorithm in
particular was recovered by instrumenting the real app, and a second
from-scratch implementation would risk being subtly wrong for no benefit.

Everything *above* this layer is new. Treat this package as an external
dependency that happens to live in-tree - domain logic belongs in services/.
"""
# protocol.py does `from . import VERSION`, so this must be bound before the
# import below or the package cycles on itself.
VERSION = "0.1.0"

from .protocol import DreameVacuumProtocol  # noqa: E402,F401
from .signing import sign_params  # noqa: E402,F401

__all__ = ["VERSION", "DreameVacuumProtocol", "sign_params"]
