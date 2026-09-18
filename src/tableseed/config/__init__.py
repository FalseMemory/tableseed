"""配置加载与校验。"""

from .checker import check_config
from .loader import load_config, load_config_from_text, load_config_text_from_file

__all__ = ["check_config", "load_config", "load_config_from_text",
           "load_config_text_from_file"]
