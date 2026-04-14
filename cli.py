#!/usr/bin/env python3
"""CLI 入口脚本"""
import sys
import os

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 直接导入，绕过 src/__init__.py（避免加载 FastAPI）
from src.cli.main import main

if __name__ == "__main__":
    main()