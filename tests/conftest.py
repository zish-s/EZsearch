"""Make the repo root importable so `import ezsearch_core` works without installing."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
