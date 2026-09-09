"""TLS context that works regardless of how Python was installed.

BUG-20260909-035. The first live run on the operator's machine failed with
`SSL: CERTIFICATE_VERIFY_FAILED ... unable to get local issuer certificate`,
seven times, with correct backoff, and a gap opened each time blaming "stream
error". The stream was fine. The Python from python.org on macOS ships with
NO root certificates wired up until the operator runs the
`Install Certificates.command` that comes with it -- a step nothing in this
project mentioned, because the earlier smoke test ran under a conda Python
that bundles its own.

Two things are done here:

  1. Prefer `certifi`'s CA bundle when it is installed (it is now a declared
     dependency), so a bare python.org install verifies certificates without
     the operator knowing the installer exists.
  2. Recognise a certificate-verification failure for what it is -- a LOCAL
     configuration problem, not an upstream outage -- and say what fixes it,
     once, instead of reconnecting for 78 seconds with an opaque message.
"""

from __future__ import annotations

import platform
import ssl
import sys


def ssl_context() -> ssl.SSLContext:
    """A verifying context, using certifi's bundle when available."""
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(cafile=certifi.where())
    except ImportError:
        pass
    except (OSError, ssl.SSLError):
        # certifi present but its bundle unreadable: fall back to the system
        # store rather than fail here. If that store is also empty the
        # connection will fail, and is_cert_failure() below will say why.
        pass
    return ctx


def is_cert_failure(exc: BaseException) -> bool:
    """True for the family of 'I cannot verify this certificate' errors."""
    if isinstance(exc, ssl.SSLCertVerificationError):
        return True
    text = f"{type(exc).__name__}: {exc}"
    return "CERTIFICATE_VERIFY_FAILED" in text or "certificate verify failed" in text


def cert_failure_hint() -> str:
    """Operator-facing explanation. Plain language, names the actual fix."""
    lines = [
        "This is NOT an OpenSea outage. Python on this machine cannot verify",
        "ANY secure website, because it has no root certificates configured.",
        "Reconnecting will not help; nothing has changed on the other end.",
        "",
        "Fix (one of):",
    ]
    if platform.system() == "Darwin":
        v = f"{sys.version_info.major}.{sys.version_info.minor}"
        lines += [
            f"  * Finder -> Applications -> Python {v} -> double-click",
            "    'Install Certificates.command', then start again. (The Python",
            "    from python.org ships without certificates until this is run.)",
        ]
    lines += [
        "  * or:  pip install certifi   -- this project now depends on it, so",
        "    re-running start.command installs it and picks it up.",
    ]
    return "\n".join(lines)
