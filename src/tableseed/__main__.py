"""让 ``python -m tableseed`` 可用。

有 ``[project.scripts]`` 装了 ``tableseed`` 命令，但在没装进环境的场景
（源码目录里直接跑、或 venv 里只装了依赖）``python -m tableseed``
是唯一入口 —— 缺这个文件会报
``No module named tableseed.__main__; 'tableseed' is a package and cannot be directly executed``。
"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
