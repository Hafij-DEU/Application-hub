# Windows launcher (no console window): double-click, or pythonw bin\app-hub.pyw [--page updates] [FILE]
import os
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))
os.environ.setdefault("PYTHONPATH", str(SRC))      # the elevated helper starts as `python -m app_hub`
from app_hub.cli import main  # noqa: E402

sys.exit(main())
