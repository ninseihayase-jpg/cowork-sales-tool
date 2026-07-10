"""pytest共通設定。リポジトリルートをsys.pathに載せ、`from cowork import ...` を可能にする。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
