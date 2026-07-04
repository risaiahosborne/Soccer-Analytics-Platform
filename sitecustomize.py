import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
SOCCER_SYSTEM_ROOT = PROJECT_ROOT / "ss_env" / "SoccerSystem"

if str(SOCCER_SYSTEM_ROOT) not in sys.path:
    sys.path.insert(0, str(SOCCER_SYSTEM_ROOT))
