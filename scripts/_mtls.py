"""Backwards-compat shim — the implementation moved to ``ai_test_gen.mtls``.

The step0 verification scripts import this module via a sys.path hack
(``sys.path.insert(0, scriptdir); import _mtls``). Keep this thin re-export so the
scripts and the package's agents share ONE source of truth for the gateway
mTLS / proxy configuration. New code should import ``ai_test_gen.mtls`` directly.
"""
from ai_test_gen.mtls import (
    describe,
    describe_trust_env,
    get_cert_arg,
    get_trust_env,
    get_verify_arg,
)

__all__ = [
    "describe",
    "describe_trust_env",
    "get_cert_arg",
    "get_trust_env",
    "get_verify_arg",
]
