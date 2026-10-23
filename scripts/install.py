"""固定版本的 4070 部署器；仅下载、安装依赖，不运行生成任务。"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import tarfile
import time
from urllib.parse import quote, urlsplit
import uuid
import zipfile


ROOT = Path(__file__).resolve().parents[1]
GIB = 1024 ** 3


def file_snapshot(path):
    info = Path(path).stat()
    return {"size": info.st_size, "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns}


def verified_snapshot(path, asset, record):
    # 快检只复用本机已完成哈希的同一文件快照；搬家、重写或旧缓存会要求重新校验。
    try:
        return (isinstance(record, dict) and record.get("sha256") == asset["sha256"].lower()
                and all(record.get(key) == value for key, value in file_snapshot(path).items())
                and record.get("size") == asset["size"])
    except (OSError, KeyError, TypeError, ValueError):
        return False


class InstallError(RuntimeError):
    pass


class Cancelled(InstallError):
    pass


class IntegrityError(InstallError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def digest_file(path, cancel_check=None):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while True:
            if cancel_check:
                cancel_check()
            chunk = source.read(8 * 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


class Installer:
    def __init__(self, root=ROOT):
        self.root = Path(root).resolve()
        self.state_dir = self.inside("_state")
        self.cache_dir = self.inside(".cache")
        self.logs_dir = self.inside("logs")
        self.cancel_flag = self.inside("_state/cancel_download.flag")
        self.status_path = self.inside("_state/install_status.json")
        self.verified_path = self.inside("_state/verified.json")
        for path in (self.state_dir, self.cache_dir, self.logs_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.log_path = self.logs_dir / ("install-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + str(os.getpid()) + ".log")
        self.status = {"schema": 1, "pid": os.getpid(), "started_at": utc_now()}
        try:
            self.verified = read_json(self.verified_path)
        except (OSError, ValueError):
            self.verified = {}
        if not isinstance(self.verified, dict):
            self.verified = {}
        self.child = None
        self.repair = False

    def inside(self, relative):
        # 所有持久文件必须落在本包目录内，防止配置中的绝对路径或父目录跳转。
        relative = str(relative)
        if "\\" in relative or ":" in relative or "\x00" in relative:
            raise InstallError("配置路径不合法：" + relative)
        parts = PurePosixPath(relative).parts
        if not parts or PurePosixPath(relative).is_absolute() or any(p in ("..", ".") for p in parts):
            raise InstallError("配置路径不合法：" + relative)
        destination = self.root.joinpath(*parts).resolve()
        try:
            destination.relative_to(self.root)
        except ValueError as exc:
            raise InstallError("路径越过部署目录：" + relative) from exc
        return destination

    def log(self, message):
        line = "[" + utc_now() + "] " + str(message)
        print(message, flush=True)
        with self.log_path.open("a", encoding="utf-8") as target:
            target.write(line + "\n")

    def write_status(self, state, **values):
        self.status.update(values)
        self.status.update(state=state, updated_at=utc_now(), log=str(self.log_path.relative_to(self.root)))
        atomic_json(self.status_path, self.status)

    def check_cancel(self):
        if self.cancel_flag.exists():
            raise Cancelled("已暂停。临时文件已保留；使用“继续安装”或 --resume 恢复。")

    @contextmanager
    def exclusive(self):
        # 锁由操作系统释放；锁文件保留，不凭旧 PID 判断进程是否仍在运行。
        lock_path = self.inside("_state/install.lock")
        handle = lock_path.open("a+b")
        locked = False
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except (OSError, BlockingIOError) as exc:
                raise InstallError("已有本包安装器运行。请先查看部署状态，勿同时启动多个安装器。") from exc
            # 启动器持有同一安装锁，且会在释放前写下服务身份；拒绝在服务运行时覆盖文件。
            service_path = self.inside("_state/services.json")
            if service_path.exists():
                try:
                    services = read_json(service_path)
                except (OSError, ValueError) as exc:
                    raise InstallError("服务身份记录无法读取。请先使用停止服务入口核对，勿直接覆盖运行环境。") from exc
                if services:
                    raise InstallError("本包仍有服务身份记录。请先使用停止服务入口，再安装、修复或校验。")
            yield
        finally:
            if locked:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def _validate_asset(self, asset):
        expected = asset.get("sha256", "")
        size = asset.get("size", -1)
        if len(expected) != 64 or any(c not in "0123456789abcdefABCDEF" for c in expected):
            raise InstallError("锁文件缺少合法 SHA-256。")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise InstallError("锁文件缺少合法文件大小。")

    def partial_path(self, destination, asset):
        return destination.with_name("." + destination.name + "." + asset["sha256"][:16].lower() + ".partial")

    def verify(self, path, asset, use_cache=True):
        self._validate_asset(asset)
        path = Path(path)
        actual = file_snapshot(path)
        if actual["size"] != asset["size"]:
            raise IntegrityError("大小校验失败，文件保留：" + str(path) + "；预期 " + str(asset["size"]) + " 字节，实际 " + str(actual["size"]) + " 字节。")
        key = path.relative_to(self.root).as_posix()
        expected = asset["sha256"].lower()
        record = self.verified.get(key, {})
        if use_cache and verified_snapshot(path, asset, record):
            return
        self.check_cancel()
        self.write_status("verifying", current_file=key, current_bytes=actual["size"], current_total=asset["size"])
        self.log("校验 SHA-256：" + path.name)
        found = digest_file(path, self.check_cancel)
        if found != expected:
            raise IntegrityError("SHA-256 校验失败，文件保持原样，未安装：" + str(path) + "；请保留日志，核对来源后移走此文件再重试。预期 " + expected + "，实际 " + found)
        # 只缓存已校验的同一文件快照；校验时若文件被修改则不接受。
        after = file_snapshot(path)
        if actual != after:
            raise IntegrityError("校验期间文件发生变化，请关闭其他写入程序后重试：" + str(path))
        self.verified[key] = {"sha256": expected, **actual, "verified_at": utc_now()}
        atomic_json(self.verified_path, self.verified)

    def _terminate_child(self):
        # 仅结束当前安装器自己启动的子进程，不按进程名批量结束程序。
        child = self.child
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)

    def download(self, asset, destination, urls):
        self._validate_asset(asset)
        destination = Path(destination).resolve()
        destination.relative_to(self.root)
        self.check_cancel()
        if destination.exists():
            try:
                self.verify(destination, asset)
            except IntegrityError as exc:
                self.quarantine(destination, exc)
            if destination.exists():
                self.log("文件已就绪：" + destination.name)
                return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = self.partial_path(destination, asset)
        if partial.exists() and partial.stat().st_size > asset["size"]:
            self.quarantine(partial, IntegrityError("临时文件超过锁定大小。"))
        if partial.exists() and partial.stat().st_size == asset["size"]:
            try:
                self.verify(partial, asset, use_cache=False)
            except IntegrityError as exc:
                self.quarantine(partial, exc)
            if partial.exists():
                os.replace(partial, destination)
                self._cache_promoted(destination, asset)
                return destination
        curl = shutil.which("curl.exe") or shutil.which("curl")
        if not curl:
            raise InstallError("未找到 Windows 自带 curl.exe。请确认系统组件完整。")
        if not urls:
            raise InstallError("文件没有下载地址：" + destination.name)
        failures = []
        for address in urls:
            self.check_cancel()
            parsed = urlsplit(address)
            if parsed.scheme not in ("https", "http") or not parsed.netloc:
                raise InstallError("下载地址无效：" + str(address))
            # HTTP 仅允许本地测试；正式来源必须使用正常校验证书的 HTTPS。
            if parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
                raise InstallError("远程下载地址必须为 HTTPS：" + address)
            host = parsed.netloc
            self.log("下载/续传：" + destination.name + "（" + host + "）")
            redirect_protocols = "=https" if parsed.scheme == "https" else "=https,http"
            command = [curl, "--fail", "--location", "--silent", "--show-error", "--connect-timeout", "30", "--speed-time", "180", "--speed-limit", "1024", "--retry", "2", "--retry-delay", "2", "--proto", "=https,http", "--proto-redir", redirect_protocols, "--continue-at", "-", "--output", str(partial), "--url", address]
            with self.log_path.open("ab") as log_handle:
                self.child = subprocess.Popen(command, stdout=log_handle, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                try:
                    while self.child.poll() is None:
                        self.check_cancel()
                        size = partial.stat().st_size if partial.exists() else 0
                        self.write_status("downloading", current_file=str(destination.relative_to(self.root)), current_bytes=size, current_total=asset["size"], source_host=host)
                        time.sleep(0.25)
                    code = self.child.returncode
                finally:
                    self._terminate_child()
                    self.child = None
            self.check_cancel()
            if code:
                failures.append(host + "：curl " + str(code))
                self.log("此来源暂未完成，保留断点。curl 返回 " + str(code) + "，详细错误见日志。")
                continue
            # 完整文件一律校验，未通过时不尝试覆盖，也不冒充成功。
            try:
                self.verify(partial, asset, use_cache=False)
            except IntegrityError as exc:
                self.quarantine(partial, exc)
                failures.append(host + "：文件校验失败，已隔离")
                continue
            os.replace(partial, destination)
            self._cache_promoted(destination, asset)
            self.log("下载并校验完成：" + destination.name)
            return destination
        raise InstallError("下载失败；断点保留于 " + str(partial) + "。" + "；".join(failures) + "。404/TLS 等错误不会触发关闭证书校验；请查看日志后重试。")

    def quarantine(self, path, error):
        # 不删除损坏文件；改名隔离后，下次点击继续会自动重新下载。
        retained = path.with_name(path.name + ".bad-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-" + uuid.uuid4().hex[:6])
        os.replace(path, retained)
        self.verified.pop(path.relative_to(self.root).as_posix(), None)
        atomic_json(self.verified_path, self.verified)
        if self.repair:
            self.log("修复模式已隔离损坏文件：" + str(retained) + "；将仅重新获取这一文件。")
            return
        raise IntegrityError(str(error) + "\n损坏文件已保留并隔离至：" + str(retained) + "\n再次点击继续安装即可自动重新下载，无需手动删除。") from error

    def _cache_promoted(self, path, asset):
        self.verified[path.relative_to(self.root).as_posix()] = {"sha256": asset["sha256"].lower(), **file_snapshot(path), "verified_at": utc_now()}
        atomic_json(self.verified_path, self.verified)

    def archive_entries(self, archive, kind, prefix):
        # 先验证全部条目，再落盘；不要使用 extractall 或信任归档内的链接。
        prefix = str(prefix)
        if not prefix.endswith("/"):
            raise InstallError("源码归档必须锁定目录前缀。")
        self._safe_archive_parts(prefix[:-1])
        entries = []
        names = set()
        members = archive.getmembers() if kind == "tar" else archive.infolist()
        for member in members:
            name = member.name if kind == "tar" else member.filename
            self._safe_archive_parts(name.rstrip("/"))
            if kind == "tar":
                if not (member.isdir() or member.isreg()):
                    raise InstallError("拒绝归档中的链接或特殊文件：" + name)
                is_dir = member.isdir()
            else:
                mode = member.external_attr >> 16
                file_type = stat.S_IFMT(mode)
                if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise InstallError("拒绝归档中的链接或特殊文件：" + name)
                is_dir = member.is_dir()
            if name.rstrip("/") == prefix[:-1] and is_dir:
                continue
            if not name.startswith(prefix):
                raise InstallError("归档条目不属于锁定前缀：" + name)
            relative = name[len(prefix):].rstrip("/")
            parts = self._safe_archive_parts(relative)
            # Windows 大小写不敏感；重复条目可能覆盖先前验证的文件。
            folded = "/".join(parts).casefold()
            if folded in names:
                raise InstallError("拒绝归档中的重复路径：" + name)
            names.add(folded)
            entries.append((member, parts, is_dir))
        if not entries:
            raise InstallError("源码归档为空。")
        return entries

    @staticmethod
    def _safe_archive_parts(name):
        if not name or "\\" in name or ":" in name or "\x00" in name or name.startswith("/"):
            raise InstallError("归档路径无效：" + repr(name))
        parts = name.split("/")
        reserved = {"CON", "PRN", "AUX", "NUL", *("COM" + str(n) for n in range(1, 10)), *("LPT" + str(n) for n in range(1, 10))}
        for part in parts:
            if part in ("", ".", "..") or part.endswith((" ", ".")) or any(c in part for c in '<>"|?*') or part.split(".")[0].upper() in reserved:
                raise InstallError("归档路径不适用于安全 Windows 解压：" + repr(name))
        return parts

    def extract_source(self, archive_path, asset):
        destination = self.inside(asset["destination"])
        marker_name = ".h3-source.json"
        marker = destination / marker_name
        expected = {"sha256": asset["sha256"].lower(), "prefix": asset["prefix"], "kind": asset["kind"]}
        previous = {}
        if marker.exists():
            try:
                previous = read_json(marker)
            except (OSError, ValueError):
                previous = {}
            if not isinstance(previous, dict) or any(previous.get(k) != v for k, v in expected.items()):
                if not self.repair:
                    raise InstallError("源码标记损坏或版本不符。请使用修复入口恢复锁定版本，用户文件会保留：" + str(destination))
                previous = {}
            records = previous.get("files", {})
            if isinstance(records, dict) and records and all(verified_snapshot(destination / name, record, record) for name, record in records.items()):
                self.log("源码文件快照与已校验记录一致：" + asset["destination"])
                return
        elif destination.exists() and any(destination.iterdir()) and not self.repair:
            raise InstallError("源码目录非空且没有完成标记。请使用修复入口恢复锁定文件，额外文件会保留：" + str(destination))
        stage = self.inside("_state/extract-" + uuid.uuid4().hex)
        stage.mkdir()
        kind = asset["kind"]
        if kind not in ("tar", "zip"):
            raise InstallError("不支持的源码归档类型：" + str(kind))
        try:
            opener = tarfile.open(archive_path, "r:*") if kind == "tar" else zipfile.ZipFile(archive_path)
            with opener as archive:
                entries = self.archive_entries(archive, kind, asset["prefix"])
                self.write_status("extracting", current_file=asset["destination"])
                for member, parts, is_dir in entries:
                    self.check_cancel()
                    target = stage.joinpath(*parts)
                    target.resolve().relative_to(stage.resolve())
                    if is_dir:
                        target.mkdir(parents=True, exist_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        source = archive.extractfile(member) if kind == "tar" else archive.open(member)
                        if source is None:
                            raise InstallError("无法读取源码条目。")
                        with source, target.open("xb") as output:
                            shutil.copyfileobj(source, output)
            destination.parent.mkdir(parents=True, exist_ok=True)
            source_files = [parts for _, parts, is_dir in entries if not is_dir]
            if not destination.exists() or not any(destination.iterdir()):
                if destination.exists():
                    destination.rmdir()
                os.replace(stage, destination)
            else:
                changed = []
                for parts in source_files:
                    target = destination.joinpath(*parts)
                    target.resolve().relative_to(destination.resolve())
                    original = stage.joinpath(*parts)
                    if not target.is_file() or target.stat().st_size != original.stat().st_size or digest_file(target, self.check_cancel) != digest_file(original, self.check_cancel):
                        changed.append((target, original, parts))
                if changed and not self.repair:
                    raise InstallError("发现缺失或修改的锁定源码：" + str(changed[0][0]) + "。请使用修复入口；原文件将备份，用户输入和输出会保留。")
                backup = self.inside("_state/source-backups/" + uuid.uuid4().hex + "/" + asset["destination"])
                for target, original, parts in changed:
                    self.check_cancel()
                    if target.exists():
                        if not target.is_file() or target.is_symlink():
                            raise InstallError("修复目标不是普通文件，拒绝覆盖目录或链接：" + str(target))
                        retained = backup.joinpath(*parts)
                        retained.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(target, retained)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(original, target)
            # 完成标记带文件清单；以后先快检，只有文件变化时才重新读取归档和比较源码。
            records = {}
            for parts in source_files:
                target = destination.joinpath(*parts)
                records["/".join(parts)] = {"sha256": digest_file(target, self.check_cancel), **file_snapshot(target)}
            atomic_json(marker, {**expected, "schema": 2, "files": records})
            self.log("源码解压完成：" + asset["destination"])
        finally:
            if stage.exists():
                shutil.rmtree(stage)

    def prepare_sources(self, runtime):
        for archive in runtime["archives"]:
            # Python 由启动脚本先行部署，安装器只处理另外两份固定源码。
            if archive["destination"] == "runtime":
                continue
            archive_file = self.inside(".cache/archives/" + archive["file"])
            self.download(archive, archive_file, [archive["url"]])
            self.extract_source(archive_file, archive)

    def model_urls(self, model, routes):
        return [route.rstrip("/") + "/" + quote(model["repo"], safe="/") + "/resolve/" + quote(model["revision"], safe="") + "/" + quote(model["path"], safe="/") + "?download=true" for route in routes]

    def download_models(self, models):
        for model in models["files"]:
            self.download(model, self.inside(model["destination"]), self.model_urls(model, models["routes"]))

    def remaining(self, destination, asset):
        self._validate_asset(asset)
        if destination.exists():
            return max(0, asset["size"] - destination.stat().st_size)
        partial = self.partial_path(destination, asset)
        return max(0, asset["size"] - (partial.stat().st_size if partial.exists() else 0))

    def disk_preflight(self, runtime, models=None, wheels=None, prepare_only=False, packages=None):
        required = 0
        if models is not None:
            required += sum(self.remaining(self.inside(m["destination"]), m) for m in models["files"])
        if wheels is not None:
            required += sum(self.remaining(self.inside(".cache/wheels/" + w["file"]), w) for w in wheels["wheels"])
        if packages is not None:
            required += sum(self.remaining(self.inside(".cache/wheels/" + w["file"]), w) for w in packages["wheels"])
        if runtime is not None:
            required += sum(self.remaining(self.inside(".cache/archives/" + a["file"]), a) for a in runtime["archives"] if a["destination"] != "runtime")
        # 预留依赖解包、pip 缓存和目标电脑首次启动的基本空间。
        required += 256 * 1024 * 1024 if prepare_only else 15 * GIB
        available = shutil.disk_usage(self.root).free
        self.log("磁盘检查：需要约 " + format(required / GIB, ".2f") + " GiB，可用 " + format(available / GIB, ".2f") + " GiB。")
        if available < required:
            raise InstallError("部署目录所在磁盘空间不足；请至少腾出 " + format((required - available) / GIB, ".2f") + " GiB 后继续。已有断点已计入。")

    def _run_python(self, args, capture=False):
        self.check_cancel()
        command = [sys.executable, *args]
        self.log("执行环境步骤：python " + " ".join(str(x) for x in args[:4]))
        # pip 日志直接落文件，不把大段安装输出塞入状态 JSON。
        output_path = self.inside("_state/process-" + uuid.uuid4().hex + ".log") if capture else self.log_path
        try:
            with output_path.open("ab") as output:
                # 避免用户全局 pip 配置额外注入索引或自动更换锁定来源。
                environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("PIP_")}
                environment["PIP_CONFIG_FILE"] = os.devnull
                environment["PYTHONUTF8"] = "1"
                environment["PYTHONIOENCODING"] = "utf-8"
                self.child = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT, cwd=self.root, env=environment, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                try:
                    while self.child.poll() is None:
                        self.check_cancel()
                        time.sleep(0.25)
                    code = self.child.returncode
                finally:
                    self._terminate_child()
                    self.child = None
            result = output_path.read_text(encoding="utf-8", errors="replace") if capture else ""
            if capture:
                with self.log_path.open("a", encoding="utf-8") as target:
                    target.write(result)
            if code:
                raise InstallError("依赖步骤失败（退出码 " + str(code) + "），未自动更换版本。请查看日志：" + str(self.log_path))
            return result
        finally:
            if capture and output_path.exists():
                output_path.unlink()

    def dependency_signature(self):
        files = ("locks/runtime.json", "locks/requirements.txt", "locks/torch-wheels.json", "locks/package-wheels.json")
        digest = hashlib.sha256()
        for name in files:
            digest.update(name.encode("utf-8"))
            digest.update(self.inside(name).read_bytes())
        return digest.hexdigest()

    def install_dependencies(self, runtime, wheels, packages):
        expected_python = self.inside("runtime/python.exe")
        if Path(sys.executable).resolve() != expected_python:
            raise InstallError("完整安装必须使用本包 runtime/python.exe；请从包内的一键安装入口启动，避免修改其他 Python 环境。")
        signature = self.dependency_signature()
        marker = self.inside("_state/deps_ready.json")
        try:
            old = read_json(marker) if marker.exists() else {}
        except (OSError, ValueError):
            old = {}
        incomplete = self.inside("_state/deps_incomplete.flag")
        force_reinstall = incomplete.exists()
        if old.get("signature") == signature:
            try:
                frozen = self._run_python(["-m", "pip", "freeze", "--all"], capture=True)
            except Cancelled:
                raise
            except InstallError:
                frozen = ""
                force_reinstall = True
                self.log("pip 无法读取旧环境，将重新引导并从锁定缓存恢复。")
            if not force_reinstall and hashlib.sha256(frozen.encode("utf-8")).hexdigest() == old.get("freeze_sha256"):
                try:
                    self._run_python(["-m", "pip", "check"])
                    self.log("依赖版本和 pip check 与成功记录一致，跳过重复安装。")
                    return
                except Cancelled:
                    raise
                except InstallError:
                    force_reinstall = True
                    self.log("依赖检查失败，将从锁定缓存重装依赖。")
        self.write_status("installing_dependencies", current_file="固定 Python 依赖")
        wheel_directory = self.inside(".cache/wheels")
        for manifest in (wheels, packages):
            if manifest.get("index_url", "https://pypi.org/simple") != "https://pypi.org/simple":
                raise InstallError("依赖索引必须为锁定的 https://pypi.org/simple。")
            for wheel in manifest["wheels"]:
                self.download(wheel, self.inside(".cache/wheels/" + wheel["file"]), [wheel["url"]])
        constraints = self.inside("_state/torch-constraints.txt")
        constraints.write_text("".join(name + "==" + version + "\n" for name, version in runtime["torch"].items()), encoding="utf-8")
        incomplete.write_text(utc_now() + "\n", encoding="utf-8")
        marker.unlink(missing_ok=True)
        # NuGet Python 带 ensurepip；先引导 pip，再固定 pip 自身版本。
        self._run_python(["-m", "ensurepip", "--upgrade"])
        self._run_python(["-m", "pip", "install", "--disable-pip-version-check", "--no-index", "--find-links", str(wheel_directory), "pip==" + runtime["pip_version"]])
        # 所有依赖均先续传、验大小及 SHA；resolver 只允许从本地闭包选包。
        self._run_python(["-m", "pip", "install", "--disable-pip-version-check", "--no-index", "--find-links", str(wheel_directory), *(["--force-reinstall"] if force_reinstall else []), "--constraint", str(constraints), "--requirement", str(self.inside("locks/requirements.txt"))])
        self._run_python(["-m", "pip", "check"])
        frozen = self._run_python(["-m", "pip", "freeze", "--all"], capture=True)
        self.inside("_state/pip-freeze.txt").write_text(frozen, encoding="utf-8")
        atomic_json(marker, {"signature": signature, "freeze_sha256": hashlib.sha256(frozen.encode("utf-8")).hexdigest(), "python": sys.version, "completed_at": utc_now(), "runtime_sha256": digest_file(self.inside("locks/runtime.json")), "requirements_sha256": digest_file(self.inside("locks/requirements.txt"))})
        incomplete.unlink(missing_ok=True)
        self.log("依赖安装及 pip check 完成；尚未加载模型。")

    def run(self, prepare_only=False, models_only=False, resume=False, repair=False):
        with self.exclusive():
            self.repair = repair
            # 只有确实拿到互斥锁的恢复操作才可清除暂停信号。
            if resume:
                self.cancel_flag.unlink(missing_ok=True)
            try:
                self.check_cancel()
                self.write_status("starting", mode="prepare_only" if prepare_only else "models_only" if models_only else "full")
                runtime = read_json(self.inside("locks/runtime.json"))
                models = None if prepare_only else read_json(self.inside("locks/models.json"))
                wheels = None if prepare_only or models_only else read_json(self.inside("locks/torch-wheels.json"))
                packages = None if prepare_only or models_only else read_json(self.inside("locks/package-wheels.json"))
                self.disk_preflight(None if models_only else runtime, models, wheels, prepare_only, packages)
                if not prepare_only and not models_only:
                    self.log("检查目标电脑硬件；不会加载 GPU 模型。")
                    self._run_python(["-I", str(self.inside("scripts/manage.py")), "hardware"])
                if not models_only:
                    self.prepare_sources(runtime)
                if prepare_only:
                    self.write_status("prepared", message="源码已准备；未安装依赖、未下载模型、未生成视频。")
                    self.log("源码准备完成；未安装依赖、未下载模型。")
                    return
                if not models_only:
                    self.install_dependencies(runtime, wheels, packages)
                self.download_models(models)
                if models_only:
                    self.write_status("models_ready", message="模型下载与校验完成；环境是否就绪以完整安装结果为准。")
                    self.log("模型下载与校验完成；环境是否就绪以完整安装结果为准。")
                else:
                    self.log("复核锁定依赖、源码、模型校验记录与硬件条件；不会加载模型。")
                    self._run_python(["-I", str(self.inside("scripts/manage.py")), "check"])
                    self.write_status("ready", message="环境与模型就绪，等待目标4070首测", target_tested=False)
                    self.log("环境与模型就绪，等待目标4070首测")
            except Cancelled as exc:
                self.write_status("paused", error=str(exc))
                self.log(str(exc))
                raise
            except Exception as exc:
                self.write_status("error", error=str(exc))
                self.log("部署未完成：" + str(exc))
                raise


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="4070 便携环境与模型安装器；不会自动生成视频。")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--prepare-only", action="store_true", help="只下载并安全解压固定源码")
    mode.add_argument("--models-only", action="store_true", help="只下载/续传并校验模型")
    mode.add_argument("--pause", action="store_true", help="请求正在运行的本包安装器暂停")
    parser.add_argument("--resume", action="store_true", help="获得安装锁后清除暂停标志并继续")
    parser.add_argument("--repair", action="store_true", help="恢复缺失/损坏的锁定源码与模型，复用完整缓存并保留备份，不删除用户输入输出")
    args = parser.parse_args(argv)
    installer = Installer()
    if args.pause:
        if args.resume:
            parser.error("--pause 与 --resume 不能同时使用")
        installer.cancel_flag.write_text(utc_now() + "\n", encoding="utf-8")
        installer.log("已发送暂停请求。本包安装器会保留断点；不会结束其他程序。")
        return 0
    try:
        installer.run(args.prepare_only, args.models_only, args.resume, args.repair)
        return 0
    except Cancelled:
        return 2
    except KeyboardInterrupt:
        installer._terminate_child()
        installer.write_status("paused", error="用户中断；断点已保留。")
        installer.log("用户中断；断点已保留。")
        return 2
    except Exception as exc:
        installer.log(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
