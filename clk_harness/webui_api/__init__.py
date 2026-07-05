"""Web-UI REST surface, decomposed from ``clk_harness/webui_router.py``.

Importing this package registers every endpoint module on the shared
:data:`router`:

* :mod:`.router` — the ``APIRouter`` + shared guards/helpers, workspace
  config, global ``.env``, doctor, idea, and provider listing.
* :mod:`.events` — activity history, harness log tail, snapshot, and
  the SSE activity stream.
* :mod:`.files` — workspace file listing/read/write/download and the
  git-history endpoints.

The provider probe/discovery endpoints stay in the legacy
``clk_harness.webui_router`` shim (see the note there).
"""

from . import events as events
from . import files as files
from .router import router as router

__all__ = ["router", "events", "files"]
