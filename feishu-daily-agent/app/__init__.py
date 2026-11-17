"""Legacy channel entrypoint compatibility for a source checkout."""
from pathlib import Path
import sys
_product_root = str(Path(__file__).resolve().parents[2])
if _product_root not in sys.path:
    sys.path.insert(0, _product_root)
