from pathlib import Path
import argparse
from contextlib import contextmanager
import ctypes
import importlib.metadata
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
import uuid


ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "_state"
LOGS = ROOT / "logs"
PYTHON = ROOT / "runtime/python.exe"
CONFIG = json.loads((ROOT / "locks/runtime.json").read_text(encoding="utf-8"))


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def verified_snapshot(path, asset, record):
    try:
        info = path.stat()
        return (isinstance(record, dict) and record.get("sha256") == asset["sha256"].lower()
                and record.get("size") == asset["size"] == info.st_size
                and record.get("mtime_ns") == info.st_mtime_ns and record.get("ctime_ns") == info.st_ctime_ns)
    except (OSError, KeyError, TypeError, ValueError):
        return False


def asset_errors():
    # 启动检查只读元数据，不反复读取几十 GB；文件变化后需通过安装入口重新做 SHA-256 校验。
    errors = []
    try:
        verified = json.loads((STATE / "verified.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        verified = {}
    models = json.loads((ROOT / "locks/models.json").read_text(encoding="utf-8"))
    for item in models["files"]:
        path = ROOT / item["destination"]
        record = verified.get(item["destination"], {}) if isinstance(verified, dict) else {}
        if not verified_snapshot(path, item, record):
            errors.append("模型缺失、文件变化或没有本机 SHA-256 校验记录：" + path.name + "。请运行安装或修复入口。")
    for asset in CONFIG["archives"]:
        if asset["destination"] == "runtime":
            continue
        folder = ROOT / asset["destination"]
        try:
            marker = json.loads((folder / ".h3-source.json").read_text(encoding="utf-8"))
            records = marker["files"]
            if not isinstance(records, dict) or not records or any(marker.get(k) != asset[k] for k in ("sha256", "prefix", "kind")):
                raise ValueError("源码清单不完整")
            for name, record in records.items():
                path = (folder / name).resolve()
                path.relative_to(folder.resolve())
                if not verified_snapshot(path, record, record):
                    raise ValueError("源码文件发生变化：" + name)
        except (OSError, ValueError, KeyError, TypeError) as error:
            errors.append("源码需要校验或修复：" + asset["destination"] + "（" + str(error) + "）")
    return errors


@contextmanager
def file_lock(name, message):
    # 与安装器和 PowerShell 引导共享锁；不通过 PID 文件猜测锁是否过期。
    handle = None
    locked = False
    try:
        try:
            handle = (STATE / name).open("a+b")
            handle.seek(0, os.SEEK_END)
            if not handle.tell():
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except (OSError, BlockingIOError) as error:
            raise RuntimeError(message) from error
        yield
    finally:
        if handle is not None:
            if locked:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def memory_info():
    class MemoryStatus(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
            (name, ctypes.c_ulonglong) for name in (
                "total_physical", "available_physical", "total_pagefile", "available_pagefile",
                "total_virtual", "available_virtual", "available_extended_virtual")]
    status = MemoryStatus()
    status.length = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise OSError("无法读取 Windows 内存状态")
    return {name: getattr(status, name) for name in (
        "total_physical", "available_physical", "available_pagefile")}


def check_environment(hardware_only=False):
    errors, warnings = [], []
    if sys.platform != "win32" or sys.getwindowsversion().build < CONFIG["minimum_windows_build"]:
        errors.append("当前固定运行时需要 Windows 11 64 位。")
    if sys.maxsize <= 2 ** 32:
        errors.append("需要 64 位 Python。")
    if sys.version_info[:3] != tuple(map(int, CONFIG["python_version"].split("."))):
        errors.append("独立 Python 版本与锁定版本不一致，请重新安装。")
    if not hardware_only:
        for line in (ROOT / "locks/requirements.txt").read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            name, expected = line.strip().split("==", 1)
            try:
                actual = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                actual = "缺失"
            if actual != expected:
                errors.append(f"依赖 {name}：需要 {expected}，当前 {actual}。请重新运行安装入口。")
        errors.extend(asset_errors())

    gpu_text, memory = "", {}
    try:
        result = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
                                 "--format=csv,noheader,nounits"], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=15, check=True,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        gpu_text = result.stdout.strip()
        gpu = gpu_text.splitlines()[0].rsplit(",", 2)
        if int(float(gpu[1].strip())) < 7800:
            errors.append("此配置需要约 8GB 显存。")
        if int(gpu[2].strip().split(".")[0]) < CONFIG["minimum_driver_major"]:
            errors.append("NVIDIA 驱动过旧，需要 r580 或更新版本。")
        if "4070 Laptop" not in gpu[0]:
            warnings.append("检测到的显卡不是 4070 Laptop；此包尚未在该显卡验证。")
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        errors.append("无法查询 NVIDIA 显卡，请安装适用的 NVIDIA 驱动后重试。")
    if sys.platform == "win32":
        memory = memory_info()
        if memory["total_physical"] < 30 * 1024 ** 3:
            errors.append("此配置面向 32GB 系统内存，检测到容量不足。")
        if memory["available_physical"] < 4 * 1024 ** 3:
            warnings.append("当前可用内存少于 4GB，生成前请关闭暂时不用的程序。")
        if memory["available_pagefile"] < 8 * 1024 ** 3:
            errors.append("Windows 可提交内存余量不足 8GB，请释放内存或检查分页文件。")
    result = {"environment_ready": not errors, "hardware_only": hardware_only, "inference_verified": False,
              "gpu": gpu_text, "memory": memory, "errors": errors, "warnings": warnings}
    save_json(STATE / ("hardware_check.json" if hardware_only else "environment_check.json"), result)
    label = "硬件检查：" if hardware_only else "环境检查："
    passed = "符合本包硬件条件" if hardware_only else "可进入首次生成测试"
    print(label + (passed if not errors else "尚未就绪"))
    if gpu_text:
        print(gpu_text)
    for message in errors:
        print("缺项：" + message)
    for message in warnings:
        print("提示：" + message)
    print("此检查不加载 GPU 模型；生成效果和目标机运行仍需首次验收。")
    return result


def port_available(port):
    with socket.socket() as connection:
        try:
            connection.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def request_json(port, path):
    # 本机请求绕过系统代理，避免本地接口被代理转发。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{port}{path}", timeout=3) as response:
        return json.load(response)


def owned_process(record):
    import psutil
    try:
        if not isinstance(record, dict) or record.get("script") not in ("scripts/run_comfy.py", "app/server.py"):
            return None
        process = psutil.Process(record["pid"])
        if (Path(process.exe()).resolve() == PYTHON.resolve()
                and abs(process.create_time() - record["created"]) < 0.1
                and str(ROOT / record["script"]) in process.cmdline() and process.is_running()):
            return process
        return None
    except psutil.AccessDenied as error:
        raise RuntimeError("无权核对本包服务进程，请保留状态文件后重试。") from error
    except (psutil.NoSuchProcess, psutil.ZombieProcess, KeyError, OSError, TypeError, ValueError):
        return None


def process_matches(record):
    return owned_process(record) is not None


def stored_services():
    path = STATE / "services.json"
    records = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(records, dict):
        raise RuntimeError("服务身份记录格式无效，请保留 _state/services.json 供诊断，未结束任何进程。")
    return records


def stop_services(services=None):
    services = stored_services() if services is None else services
    if not services:
        print("没有本包正在运行的服务。")
        return
    import psutil
    remaining = {}
    for name, record in services.items():
        try:
            process = owned_process(record)
            if process is None:
                continue
            process.terminate()
            try:
                process.wait(timeout=15)
            except psutil.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            print("已停止：" + name)
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except (psutil.AccessDenied, psutil.TimeoutExpired, RuntimeError) as error:
            remaining[name] = record
            print("未能停止 " + name + "：" + str(error))
    if remaining:
        save_json(STATE / "services.json", remaining)
        raise RuntimeError("部分服务未能停止，身份记录已保留，请重试停止入口。")
    (STATE / "services.json").unlink(missing_ok=True)


def launch(name, relative, arguments):
    import psutil
    env = os.environ.copy()
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONNOUSERSITE="1",
               HF_HUB_DISABLE_TELEMETRY="1", DO_NOT_TRACK="1")
    LOGS.mkdir(exist_ok=True)
    with (LOGS / (name + ".log")).open("ab") as output:
        process = subprocess.Popen([str(PYTHON), "-I", "-u", str(ROOT / relative), *arguments],
                                   cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=subprocess.STDOUT,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
    return {"pid": process.pid, "created": psutil.Process(process.pid).create_time(), "script": relative}


def wait_ready(record, port, path, timeout):
    deadline = time.monotonic() + timeout
    last_message = 0.0
    while time.monotonic() < deadline:
        if not process_matches(record):
            raise RuntimeError("服务提前退出，请查看 logs 下的对应日志。")
        try:
            return request_json(port, path)
        except (OSError, ValueError, urllib.error.URLError):
            if time.monotonic() - last_message > 20:
                print("正在启动本地服务，请稍候。", flush=True)
                last_message = time.monotonic()
            time.sleep(1)
    raise RuntimeError("服务启动超时，请查看日志；本次启动的进程将结束。")


def start_services():
    previous = stored_services()
    if previous and all(process_matches(r) for r in previous.values()) and {"comfy", "web"} <= previous.keys():
        webbrowser.open(f"http://127.0.0.1:{CONFIG['ports']['web']}/")
        print("服务已运行，已打开生成页面。")
        return
    if previous:
        # 上次只剩一项服务时，清理本包残留后重新启动，避免端口自占用。
        stop_services(previous)
    if not check_environment()["environment_ready"]:
        raise RuntimeError("环境尚未就绪，请按上面的提示处理。")
    for port in CONFIG["ports"].values():
        if not port_available(port):
            raise RuntimeError(f"端口 {port} 已被占用；请先使用停止服务入口，或关闭占用该端口的程序。")
    services = {}
    try:
        services["comfy"] = launch("comfy", "scripts/run_comfy.py", [])
        save_json(STATE / "services.json", services)
        wait_ready(services["comfy"], CONFIG["ports"]["comfy"], "/system_stats", 180)
        info = request_json(CONFIG["ports"]["comfy"], "/object_info")
        graph = json.loads((ROOT / "workflows/h3lite_4070_i2v_api.json").read_text(encoding="utf-8"))
        missing = sorted({n["class_type"] for n in graph.values()} - info.keys())
        if missing:
            raise RuntimeError("缺少工作流节点：" + ", ".join(missing))
        services["web"] = launch("web", "app/server.py", ["--root", str(ROOT), "--port",
                                  str(CONFIG["ports"]["web"]), "--comfy-port", str(CONFIG["ports"]["comfy"])])
        save_json(STATE / "services.json", services)
        wait_ready(services["web"], CONFIG["ports"]["web"], "/api/health", 30)
        url = f"http://127.0.0.1:{CONFIG['ports']['web']}/"
        webbrowser.open(url)
        print("已启动：" + url)
        print("关闭浏览器后后台仍运行；结束时使用 4_停止服务.cmd。")
    except BaseException:
        stop_services(services)
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("start", "stop", "check", "hardware"))
    options = parser.parse_args()
    STATE.mkdir(exist_ok=True)
    if options.action in ("check", "hardware"):
        return 0 if check_environment(hardware_only=options.action == "hardware")["environment_ready"] else 1
    if options.action == "stop":
        with file_lock("services.lock", "服务正在启动或停止，请稍后再试。"):
            stop_services()
    else:
        with file_lock("bootstrap.lock", "独立 Python 正在部署，请等待安装结束。"), file_lock(
                "install.lock", "本包正在安装或修复，请等待完成。"), file_lock(
                "services.lock", "服务正在启动或停止，请稍后再试。"):
            start_services()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError) as error:
        print("操作未完成：" + str(error), file=sys.stderr)
        raise SystemExit(1)
