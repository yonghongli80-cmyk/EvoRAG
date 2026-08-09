"""RAG 知识库系统 — 一键启动脚本"""

import os
import subprocess
import sys
import time
from pathlib import Path


def main():
    print()
    print("  ╔══════════════════════════════════╗")
    print("  ║   RAG 知识库系统                 ║")
    print("  ╚══════════════════════════════════╝")
    print()
    print("  启动中... 请稍候")
    print()

    project_root = Path(__file__).resolve().parent

    # Docker 环境用 0.0.0.0，本地用 127.0.0.1
    host = "0.0.0.0" if os.getenv("DOCKER_CONTAINER") else "127.0.0.1"

    # 非回环地址绑定 + 认证关闭时强制告警（不阻断启动）
    if host not in ("127.0.0.1", "localhost"):
        try:
            from config import settings

            auth_enabled = settings.auth_enabled
        except Exception:
            auth_enabled = False
        if not auth_enabled:
            print(
                f"  [ERROR] ⚠️ 认证已关闭，服务绑定 {host}，存在未授权访问风险。"
                "上线前请在 .env 设置 RAG_AUTH_ENABLED=true"
            )

    # 启动 API
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "rag.api:app", "--host", host, "--port", "8080"],
        cwd=str(project_root),
    )

    # 等待 API 就绪
    import urllib.request

    for i in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:8080/health", timeout=1)
            break
        except Exception:
            time.sleep(0.5)
    else:
        print("  [警告] API 启动超时，请检查终端输出")

    print("  ╔══════════════════════════════════╗")
    print("  ║   服务已就绪！                    ║")
    print(f"  ║   http://{host}:8080              ║")
    print("  ║   关闭此窗口停止服务              ║")
    print("  ╚══════════════════════════════════╝")
    print()

    # Docker 环境不打开浏览器
    if host == "127.0.0.1":
        import webbrowser
        webbrowser.open("http://localhost:8080")

    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        print("\n  服务已停止。")


if __name__ == "__main__":
    main()
