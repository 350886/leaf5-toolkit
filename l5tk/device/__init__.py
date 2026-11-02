"""device 层：只回答「这是什么机器、它有什么前提」，不执行动作。"""

from .detect import describe, detect, ensure_known
from .models import Detection, Profile
from .profiles import GENERIC, PROFILES, TAB_FAMILY, all_profiles, by_key, table

__all__ = ["detect", "describe", "ensure_known", "Detection", "Profile",
           "PROFILES", "GENERIC", "TAB_FAMILY", "by_key", "all_profiles", "table"]
