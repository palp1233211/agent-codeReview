#!/usr/bin/env python3
"""CLI 入口脚本"""
import sys
import os
import warnings
warnings.filterwarnings("ignore", category=UserWarning, module="pkg_resources")
# lark_oapi 的 ws 子模块在 import 时触发这条 pkg_resources 弃用提示，
# 但告警定位到调用方文件（lark_oapi/ws/pb/google/__init__.py），module 过滤匹配不上，
# 改用 message 匹配才能真正消掉。
warnings.filterwarnings("ignore", message="pkg_resources is deprecated.*", category=UserWarning)

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 直接导入，绕过 src/__init__.py（避免加载 FastAPI）
from src.cli.main import main

if __name__ == "__main__":
    main()