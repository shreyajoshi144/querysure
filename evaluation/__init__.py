"""
QuerySure evaluation layer.

A reproducible harness that measures whether QuerySure's NL -> SQL pipeline
actually works, and quantifies hallucination/reliability. It drives the
*existing* pipeline modules (retrieval -> generation -> validation ->
execution) and never modifies them.

Run everything from the repository root:

    python -m evaluation run --mode oracle        # harness self-check (offline)
    python -m evaluation run --mode synthetic     # defect-injected demo (offline)
    python -m evaluation live                     # REAL LLM -- needs API keys

See evaluation/README.md for metric definitions and limitations.
"""
from __future__ import annotations

import os
import sys

__version__ = "1.1.0"

# The pipeline modules (`modules`, `config`, ...) live at the repository root
# and are imported as top-level packages by the app itself. Make that work
# whether the harness is started as `python -m evaluation` from the root or
# imported from a test process.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
