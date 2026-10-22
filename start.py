"""RAG 知识库系统 — 一键启动脚本

容器里本脚本是 PID 1（见 ``Dockerfile`` 的 exec-form ``CMD``），因此它必须自己做两件事：

1. **转发停止信号**：``docker stop`` 只把 SIGTERM 交给 PID 1；若不转发，uvicorn 子进程
   看不到信号，``proc.wait()`` 会被 EINTR 打断后继续等（POSIX 语义），容器在宽限期后被
   SIGKILL——FastAPI 的 lifespan 清理（worker 退出、WAL checkpoint、临时文件）全部跳过。
2. **兜底强杀**：宽限期（``RAG_SHUTDOWN_TIMEOUT``，默认 10s）内没退出就 ``kill()``，
   避免容器卡在 ``stopping``。
"""

import contextlib
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

#: 就绪探测路径。``/health`` 同时报告存活与依赖状态；S7 增加 ``/ready`` 后可改这里
#: （改完请同步 ``deploy.sh`` 的路径探测逻辑与 ``docs/deployment.md``）。
READINESS_PATH = "/health"

#: 停止信号宽限期（秒），超时即强杀。
DEFAULT_SHUTDOWN_TIMEOUT = 10.0

#: 子进程等待的分片长度：既让信号处理器能及时插入，也避免忙等。
SHUTDOWN_POLL_INTERVAL = 0.2

#: 启动就绪探测的次数与间隔（共 30s，与原实现一致）。
DEFAULT_HEALTH_ATTEMPTS = 60
DEFAULT_HEALTH_INTERVAL = 0.5


def _port_is_available(host: str, port: int) -> bool:
    """Return whether this process can bind the requested listening address."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def _remote_deployment_error(host: str, config, db) -> str | None:
    """Return a fail-closed deployment error for unsafe remote authentication."""
    if host in ("127.0.0.1", "localhost", "::1"):
        return None
    if not config.auth_enabled:
        return "认证已关闭，不能绑定非回环地址。"
    if config.allow_initial_passwords_remote:
        return None

    from rag.auth import verify_password

    with db.locked_read() as conn:
        password_hashes = [
            row[0] for row in conn.execute(
                "SELECT u.password FROM users u JOIN org_people p ON p.user_id=u.id "
                "WHERE p.active=1"
            )
        ]
    if any(verify_password("123456", value) for value in password_hashes):
        return (
            "仍有启用的组织账号使用统一初始密码 123456，不能绑定非回环地址。"
            "请先修改密码，或明确设置 RAG_ALLOW_INITIAL_PASSWORDS_REMOTE=true 承担风险。"
        )
    return None


def _shutdown_timeout() -> float:
    """Return the grace period before the uvicorn child is killed."""
    raw = os.getenv("RAG_SHUTDOWN_TIMEOUT", "")
    if not raw:
        return DEFAULT_SHUTDOWN_TIMEOUT
    try:
        return max(float(raw), 0.0)
    except ValueError:
        return DEFAULT_SHUTDOWN_TIMEOUT


def _health_attempts() -> int:
    raw = os.getenv("RAG_START_HEALTH_ATTEMPTS", "")
    try:
        return max(int(raw), 0)
    except ValueError:
        return DEFAULT_HEALTH_ATTEMPTS


def _uvicorn_command(host: str, port: int) -> list[str]:
    return [sys.executable, "-m", "uvicorn", "rag.api:app", "--host", host, "--port", str(port)]


def _exit_status(returncode: int | None) -> int:
    """Map a child status to a shell-visible exit code (signals become 128 + n)."""
    if returncode is None:
        return 1
    if returncode < 0:
        return 128 + abs(returncode)
    return returncode


class ShutdownSupervisor:
    """Owns SIGTERM/SIGINT and forwards them to the supervised uvicorn child.

    The signal handler must stay tiny: it records the request, sends the same signal to the
    child (on Windows ``send_signal`` only accepts SIGTERM, so it falls back to
    ``terminate()``), and escalates when a second signal arrives. Waiting and the timeout
    kill happen in :meth:`wait`, i.e. in normal code rather than in a signal handler.
    """

    signals = (signal.SIGTERM, signal.SIGINT)

    def __init__(self, proc, *, timeout: float, clock=time.monotonic,
                 poll_interval: float = SHUTDOWN_POLL_INTERVAL):
        self._proc = proc
        self._timeout = timeout
        self._clock = clock
        self._poll_interval = poll_interval
        self._stop_requested_at: float | None = None
        self._previous_handlers: dict[int, object] = {}

    @property
    def stop_requested_at(self) -> float | None:
        """Monotonic timestamp of the first stop signal, or ``None``."""
        return self._stop_requested_at

    def install(self) -> bool:
        """Register the handlers; return ``False`` when the thread cannot own signals."""
        if threading.current_thread() is not threading.main_thread():
            return False
        for signum in self.signals:
            try:
                self._previous_handlers[signum] = signal.signal(signum, self.handle)
            except (ValueError, OSError):
                self.uninstall()
                return False
        return True

    def uninstall(self) -> None:
        for signum, handler in list(self._previous_handlers.items()):
            with contextlib.suppress(ValueError, OSError, TypeError):
                signal.signal(signum, handler)
        self._previous_handlers.clear()

    def handle(self, signum, _frame=None) -> None:
        """Signal handler body: forward the request, force-kill on the second one."""
        first_request = self._stop_requested_at is None
        if first_request:
            self._stop_requested_at = self._clock()
        if self._proc.poll() is not None:
            return
        if not first_request:
            # Operator pressed Ctrl+C twice / compose sent a second stop: stop waiting.
            self._kill()
            return
        try:
            if os.name == "posix":
                self._proc.send_signal(signum)
            else:
                # Windows ``Popen.send_signal`` rejects SIGINT; terminate() is equivalent here.
                self._proc.terminate()
        except (OSError, ValueError):
            self._kill()

    def _kill(self) -> None:
        with contextlib.suppress(OSError, ValueError):
            self._proc.kill()

    def wait(self) -> int | None:
        """Wait for the child, killing it once the grace period after a stop expires."""
        while True:
            try:
                return self._proc.wait(timeout=self._poll_interval)
            except subprocess.TimeoutExpired:
                pass
            if self._proc.poll() is not None:
                return self._proc.returncode
            if self._stop_requested_at is None:
                continue
            if self._clock() - self._stop_requested_at >= self._timeout:
                self._kill()
                return self._proc.wait()


def _wait_until_ready(proc, *, port: int, attempts: int = DEFAULT_HEALTH_ATTEMPTS,
                      interval: float = DEFAULT_HEALTH_INTERVAL) -> bool:
    """Poll the readiness path until it answers, the child exits, or the budget runs out."""
    url = f"http://127.0.0.1:{port}{READINESS_PATH}"
    for _ in range(max(attempts, 0)):
        if proc.poll() is not None:
            return False
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except Exception:  # noqa: BLE001 - a probe must never abort the supervisor
            time.sleep(interval)
    return False


def _print_ready_banner(host: str, port: int) -> None:
    print("  ╔══════════════════════════════════╗")
    print("  ║   服务已就绪！                    ║")
    print(f"  ║   http://{host}:{port:<5}             ║")
    print("  ║   关闭此窗口停止服务              ║")
    print("  ╚══════════════════════════════════╝")
    print()


def main() -> int:
    print()
    print("  ╔══════════════════════════════════╗")
    print("  ║   RAG 知识库系统                 ║")
    print("  ╚══════════════════════════════════╝")
    print()
    print("  启动中... 请稍候")
    print()

    project_root = Path(__file__).resolve().parent

    # Docker 环境用 0.0.0.0，本地用 127.0.0.1
    in_container = bool(os.getenv("DOCKER_CONTAINER"))
    host = "0.0.0.0" if in_container else "127.0.0.1"
    port = int(os.getenv("PORT", "8000" if in_container else "8080"))

    if not _port_is_available(host, port):
        print(f"  [错误] 端口 {port} 已被占用，服务未启动。请先关闭已有实例。")
        return 1

    # 非回环地址必须满足企业账号的最低安全条件。
    if host not in ("127.0.0.1", "localhost"):
        try:
            from config import settings
            from rag.user_db import UserDB

            security_db = UserDB(settings.users_db_path)
            try:
                deployment_error = _remote_deployment_error(host, settings, security_db)
            finally:
                security_db.close()
        except Exception as exc:
            deployment_error = f"无法完成远程部署安全检查：{exc}"
        if deployment_error:
            print(f"  [错误] {deployment_error}")
            return 1

    # 启动 API。POSIX 上给子进程独立会话：终端 Ctrl+C 只到本进程，再由此处精确转发一次，
    # 避免子进程同时收到终端信号而把"二次信号"误判成"立即强杀"。
    proc = subprocess.Popen(
        _uvicorn_command(host, port),
        cwd=str(project_root),
        start_new_session=os.name == "posix",
    )

    supervisor = ShutdownSupervisor(proc, timeout=_shutdown_timeout())
    if not supervisor.install():
        print("  [警告] 无法安装信号处理器（非主线程运行），停止信号可能不会被转发")
    try:
        ready = _wait_until_ready(proc, port=port, attempts=_health_attempts())
        if not ready:
            if proc.poll() is not None:
                print(f"  [错误] API 进程已退出（退出码 {proc.returncode}），服务未启动。")
                return proc.returncode or 1
            # F-06：探测失败时绝不打印"服务已就绪"，否则部署脚本/运维会误判成功。
            print(
                f"  [警告] API 启动超时（{READINESS_PATH} 未响应），请检查终端输出"
            )
        else:
            _print_ready_banner(host, port)
            # Docker 环境不打开浏览器
            if host == "127.0.0.1":
                webbrowser.open(f"http://localhost:{port}")

        try:
            return _exit_status(supervisor.wait())
        except KeyboardInterrupt:
            # 处理器不可用时（例如非主线程）的兜底：仍把停止请求交给子进程并等它退出。
            supervisor.handle(signal.SIGINT)
            with contextlib.suppress(Exception):
                supervisor.wait()
            print("\n  服务已停止。")
            return 0
    finally:
        supervisor.uninstall()


if __name__ == "__main__":
    raise SystemExit(main())
