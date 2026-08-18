"""Local web GUI for e2d — a friendly, fully-offline front door for `migrate`.

Runs on localhost only (binds 127.0.0.1 by default). Nothing leaves the machine.
``e2d web`` keeps uploads in ``sources/`` under the working directory and
rebuilds ``out/terraform/`` from that inbox on every Convert. Unit tests still
use ephemeral temp directories.
"""

from e2d.web.server import Sessions, serve

__all__ = ["Sessions", "serve"]
