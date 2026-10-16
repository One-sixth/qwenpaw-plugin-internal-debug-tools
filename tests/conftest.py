# -*- coding: utf-8 -*-
"""测试夹具。

通过 sys.path 添加项目根支持顶层导入（core/utils/tools），
与插件内相对导入双模式对应。不依赖 pytest-asyncio。
"""

import os
import sys

# 将项目根目录添加到 sys.path
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
