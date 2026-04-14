"""运行服务脚本"""
import asyncio
import sys

from dotenv import load_dotenv


def main():
    """运行 Code Review Agent 服务"""
    load_dotenv(override=True)

    import uvicorn
    from src.main import app

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000

    print(f"启动 Code Review Agent 服务于端口 {port}")
    print("API 文档: http://localhost:{port}/docs")

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        reload=True,
    )


if __name__ == "__main__":
    main()