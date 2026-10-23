"""只使用临时目录和本地 HTTP 的安装器测试，不运行 pip、模型或 GPU。"""

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import tarfile
import tempfile
import threading
import time
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location("h3_installer", Path(__file__).with_name("install.py"))
installer_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer_module)


class RangeServer(BaseHTTPRequestHandler):
    payload = b"4070-local-test\n" * 100000
    ranges = []
    disconnected = False
    slow_started = threading.Event()

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/missing":
            self.send_error(404)
            return
        data = self.payload
        offset = 0
        if self.headers.get("Range"):
            offset = int(self.headers["Range"].removeprefix("bytes=").split("-")[0])
            type(self).ranges.append(offset)
        self.send_response(206 if offset else 200)
        self.send_header("Content-Length", str(len(data) - offset))
        if offset:
            self.send_header("Content-Range", f"bytes {offset}-{len(data)-1}/{len(data)}")
        self.end_headers()
        try:
            if self.path == "/broken" and not type(self).disconnected:
                type(self).disconnected = True
                self.wfile.write(data[offset:offset + 100000])
                self.wfile.flush()
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            if self.path == "/slow":
                type(self).slow_started.set()
                for start in range(offset, len(data), 4096):
                    self.wfile.write(data[start:start + 4096])
                    self.wfile.flush()
                    time.sleep(0.01)
            else:
                self.wfile.write(data[offset:])
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass


class InstallerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), RangeServer)
        cls.server.daemon_threads = True
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()
        cls.base = "http://127.0.0.1:" + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join(timeout=3)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="h3-4070-tests-")
        self.root = Path(self.temp.name)
        self.installer = installer_module.Installer(self.root)
        RangeServer.ranges = []
        RangeServer.disconnected = False
        RangeServer.slow_started.clear()
        self.asset = {"size": len(RangeServer.payload), "sha256": hashlib.sha256(RangeServer.payload).hexdigest()}
        self.destination = self.root / "models" / "test.bin"

    def tearDown(self):
        self.temp.cleanup()

    def test_range_resume_and_verified_cache(self):
        self.destination.parent.mkdir()
        partial = self.installer.partial_path(self.destination, self.asset)
        partial.write_bytes(RangeServer.payload[:12345])
        self.installer.download(self.asset, self.destination, [self.base + "/ok"])
        self.assertEqual(self.destination.read_bytes(), RangeServer.payload)
        self.assertIn(12345, RangeServer.ranges)
        self.assertFalse(partial.exists())
        with mock.patch.object(installer_module, "digest_file", side_effect=AssertionError("不应重复哈希")):
            self.installer.download(self.asset, self.destination, [self.base + "/missing"])

    def test_network_interruption_keeps_resumable_partial(self):
        # 关闭 curl 内置重试，验证应用层下一次运行能续传中断文件。
        real_popen = installer_module.subprocess.Popen

        def without_retries(command, *args, **kwargs):
            command = list(command)
            command[command.index("--retry") + 1] = "0"
            return real_popen(command, *args, **kwargs)

        with mock.patch.object(installer_module.subprocess, "Popen", side_effect=without_retries):
            with self.assertRaises(installer_module.InstallError):
                self.installer.download(self.asset, self.destination, [self.base + "/broken"])
        partial = self.installer.partial_path(self.destination, self.asset)
        self.assertEqual(partial.stat().st_size, 100000)
        self.installer.download(self.asset, self.destination, [self.base + "/ok"])
        self.assertIn(100000, RangeServer.ranges)
        self.assertEqual(self.destination.read_bytes(), RangeServer.payload)

    def test_hash_failure_keeps_partial(self):
        wrong = dict(self.asset, sha256="0" * 64)
        with self.assertRaises(installer_module.IntegrityError):
            self.installer.download(wrong, self.destination, [self.base + "/ok"])
        self.assertFalse(self.destination.exists())
        partial = self.installer.partial_path(self.destination, wrong)
        self.assertFalse(partial.exists())
        quarantined = list(partial.parent.glob(partial.name + ".bad-*"))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].read_bytes(), RangeServer.payload)

    def test_corrupt_formal_file_is_never_overwritten(self):
        self.destination.parent.mkdir()
        self.destination.write_bytes(b"x" * self.asset["size"])
        with self.assertRaises(installer_module.IntegrityError):
            self.installer.download(self.asset, self.destination, [self.base + "/ok"])
        quarantined = list(self.destination.parent.glob(self.destination.name + ".bad-*"))
        self.assertEqual(quarantined[0].read_bytes(), b"x" * self.asset["size"])
        self.installer.download(self.asset, self.destination, [self.base + "/ok"])
        self.assertEqual(self.destination.read_bytes(), RangeServer.payload)

    def test_repair_recovers_only_corrupt_model_and_preserves_original(self):
        self.destination.parent.mkdir()
        broken = b"x" * self.asset["size"]
        self.destination.write_bytes(broken)
        good = self.destination.with_name("good.bin")
        good.write_bytes(RangeServer.payload)
        self.installer.verify(good, self.asset)
        self.installer.repair = True
        self.installer.download(self.asset, self.destination, [self.base + "/ok"])
        self.assertEqual(self.destination.read_bytes(), RangeServer.payload)
        self.assertEqual(next(self.destination.parent.glob("test.bin.bad-*")).read_bytes(), broken)
        with mock.patch.object(installer_module, "digest_file", side_effect=AssertionError("完整文件应复用校验缓存")):
            self.installer.download(self.asset, good, [self.base + "/missing"])

    def test_old_model_cache_is_reverified_once(self):
        self.destination.parent.mkdir()
        self.destination.write_bytes(RangeServer.payload)
        self.installer.verify(self.destination, self.asset)
        record = self.installer.verified[self.destination.relative_to(self.root).as_posix()]
        record.pop("ctime_ns")
        with mock.patch.object(installer_module, "digest_file", wraps=installer_module.digest_file) as digest:
            self.installer.verify(self.destination, self.asset)
            digest.assert_called_once()
            self.installer.verify(self.destination, self.asset)
            digest.assert_called_once()

    def test_verify_path_aliases_share_canonical_cache(self):
        self.destination.parent.mkdir()
        self.destination.write_bytes(RangeServer.payload)
        aliases = [self.root / "models" / ".." / "models" / self.destination.name]
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            # 8.3 名称可能被禁用；可用时同时复现 Windows runner 的 RUNNER~1 别名。
            short_path = ctypes.windll.kernel32.GetShortPathNameW
            short_path.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
            short_path.restype = wintypes.DWORD
            buffer = ctypes.create_unicode_buffer(32768)
            length = short_path(str(self.destination), buffer, len(buffer))
            if 0 < length < len(buffer):
                aliases.append(Path(buffer.value))
        with mock.patch.object(installer_module, "digest_file", wraps=installer_module.digest_file) as digest:
            for alias in aliases:
                self.installer.verify(alias, self.asset)
            self.installer.verify(self.destination.resolve(), self.asset)
            digest.assert_called_once()
        self.assertEqual(set(self.installer.verified), {"models/test.bin"})

    def test_verify_rejects_outside_path_before_reading_or_caching(self):
        with tempfile.TemporaryDirectory(prefix="h3-outside-") as outside:
            target = Path(outside) / "test.bin"
            target.write_bytes(RangeServer.payload)
            escaped = self.root / ".." / target.parent.name / target.name
            with mock.patch.object(installer_module, "file_snapshot", side_effect=AssertionError("不应读取包外文件")), mock.patch.object(
                    installer_module, "digest_file", side_effect=AssertionError("不应校验包外文件")):
                with self.assertRaisesRegex(installer_module.InstallError, "路径越过部署目录"):
                    self.installer.verify(escaped, self.asset)
            self.assertEqual(self.installer.verified, {})
            self.assertFalse(self.installer.verified_path.exists())

    def test_cancel_kills_only_owned_curl_and_keeps_partial(self):
        observed = []

        def download():
            try:
                self.installer.download(self.asset, self.destination, [self.base + "/slow"])
            except Exception as exc:
                observed.append(exc)

        worker = threading.Thread(target=download)
        worker.start()
        self.assertTrue(RangeServer.slow_started.wait(3))
        time.sleep(0.12)
        self.installer.cancel_flag.write_text("pause", encoding="utf-8")
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertTrue(any(isinstance(exc, installer_module.Cancelled) for exc in observed))
        self.assertIsNone(self.installer.child)
        self.assertTrue(self.installer.partial_path(self.destination, self.asset).exists())
        self.assertFalse(self.destination.exists())

    def test_lock_and_resume_order(self):
        other = installer_module.Installer(self.root)
        self.installer.cancel_flag.write_text("pause", encoding="utf-8")
        with self.installer.exclusive():
            with self.assertRaises(installer_module.InstallError):
                other.run(prepare_only=True, resume=True)
            self.assertTrue(self.installer.cancel_flag.exists())
        with other.exclusive():
            pass

    def _tar(self, entries):
        path = self.root / "source.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            for name, kind, content in entries:
                member = tarfile.TarInfo(name)
                member.type = kind
                if kind == tarfile.REGTYPE:
                    member.size = len(content)
                    archive.addfile(member, io.BytesIO(content))
                else:
                    member.linkname = content.decode("utf-8")
                    archive.addfile(member)
        return path

    def _source_asset(self, archive):
        return {"destination": "engine", "kind": "tar", "prefix": "source/", "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}

    def test_safe_extract_and_completed_marker(self):
        archive = self._tar([("source/", tarfile.DIRTYPE, b""), ("source/main.py", tarfile.REGTYPE, b"# test\n")])
        asset = self._source_asset(archive)
        self.installer.extract_source(archive, asset)
        target = self.root / "engine/main.py"
        self.assertEqual(target.read_bytes(), b"# test\n")
        target.write_bytes(b"preserve user change")
        user_file = self.root / "engine/output/user.txt"
        user_file.parent.mkdir()
        user_file.write_text("用户内容", encoding="utf-8")
        with self.assertRaises(installer_module.InstallError):
            self.installer.extract_source(archive, asset)
        self.assertEqual(target.read_bytes(), b"preserve user change")
        self.installer.repair = True
        self.installer.extract_source(archive, asset)
        self.assertEqual(target.read_bytes(), b"# test\n")
        self.assertEqual(user_file.read_text(encoding="utf-8"), "用户内容")
        self.assertEqual(next((self.root / "_state/source-backups").glob("*/engine/main.py")).read_bytes(), b"preserve user change")
        target.unlink()
        self.installer.extract_source(archive, asset)
        self.assertEqual(target.read_bytes(), b"# test\n")

    def test_rejects_traversal_links_and_wrong_prefix(self):
        for name, kind, content in [("source/../../escaped.txt", tarfile.REGTYPE, b"bad"), ("source/link", tarfile.SYMTYPE, b"../../escaped"), ("source/link", tarfile.LNKTYPE, b"../../escaped"), ("other/file", tarfile.REGTYPE, b"bad"), ("source/C:evil", tarfile.REGTYPE, b"bad"), ("source/CON.txt", tarfile.REGTYPE, b"bad")]:
            with self.subTest(name=name, kind=kind):
                archive = self._tar([(name, kind, content)])
                with self.assertRaises(installer_module.InstallError):
                    self.installer.extract_source(archive, self._source_asset(archive))
                self.assertFalse((self.root / "engine").exists())
                self.assertFalse((self.root / "escaped.txt").exists())

    def test_model_routes_are_pinned_and_encoded(self):
        model = {"repo": "user/repo", "revision": "a" * 40, "path": "folder/a b.safetensors"}
        urls = self.installer.model_urls(model, ["https://huggingface.co", "https://hf-mirror.com/"])
        self.assertIn("/resolve/" + "a" * 40 + "/folder/a%20b.safetensors", urls[0])
        self.assertTrue(urls[1].startswith("https://hf-mirror.com/user/repo/"))

    def test_remaining_counts_existing_partial(self):
        self.destination.parent.mkdir()
        self.installer.partial_path(self.destination, self.asset).write_bytes(b"x" * 12345)
        self.assertEqual(self.installer.remaining(self.destination, self.asset), self.asset["size"] - 12345)

    def test_prepare_only_never_reads_models_or_runs_pip(self):
        (self.root / "locks").mkdir()
        (self.root / "locks/runtime.json").write_text(json.dumps({"archives": []}), encoding="utf-8")
        with mock.patch.object(self.installer, "install_dependencies", side_effect=AssertionError("不应安装依赖")), mock.patch.object(self.installer, "download_models", side_effect=AssertionError("不应下载模型")):
            self.installer.run(prepare_only=True)
        status = json.loads(self.installer.status_path.read_text(encoding="utf-8"))
        self.assertEqual(status["state"], "prepared")

    def test_dependency_resolver_is_offline_after_all_manifest_downloads(self):
        runtime = {"torch": {"torch": "2.11.0+cu130", "torchvision": "0.26.0+cu130", "torchaudio": "2.11.0+cu130"}, "pip_version": "26.1.2"}
        torch_manifest = {"wheels": [dict(self.asset, file="torch-test.whl", url=self.base + "/torch")]}
        package_manifest = {"wheels": [dict(self.asset, file="pip-test.whl", url=self.base + "/pip")]}
        (self.root / "locks").mkdir()
        for name, content in [("runtime.json", runtime), ("torch-wheels.json", torch_manifest), ("package-wheels.json", package_manifest)]:
            (self.root / "locks" / name).write_text(json.dumps(content), encoding="utf-8")
        (self.root / "locks/requirements.txt").write_text("torch==2.11.0+cu130\n", encoding="utf-8")
        actions = []

        def fake_download(asset, destination, urls):
            actions.append(("download", asset["file"]))
            return destination

        def fake_run(args, capture=False):
            actions.append(("python", args))
            return "pip==26.1.2\n" if "freeze" in args else ""

        with mock.patch.object(installer_module.sys, "executable", str(self.root / "runtime/python.exe")), mock.patch.object(self.installer, "download", side_effect=fake_download), mock.patch.object(self.installer, "_run_python", side_effect=fake_run):
            self.installer.install_dependencies(runtime, torch_manifest, package_manifest)
            self.assertEqual(actions[:2], [("download", "torch-test.whl"), ("download", "pip-test.whl")])
            installs = [args for action, args in actions if action == "python" and "install" in args]
            self.assertEqual(len(installs), 2)
            self.assertTrue(all("--no-index" in args and "--find-links" in args for args in installs))
            self.assertTrue(all("--no-deps" not in args for args in installs))
            self.assertIn(("python", ["-m", "pip", "check"]), actions)
            actions.clear()
            self.installer.install_dependencies(runtime, torch_manifest, package_manifest)
            self.assertEqual(actions, [("python", ["-m", "pip", "freeze", "--all"]), ("python", ["-m", "pip", "check"])])
            actions.clear()
            (self.root / "_state/deps_incomplete.flag").write_text("interrupted", encoding="utf-8")
            self.installer.install_dependencies(runtime, torch_manifest, package_manifest)
            installs = [args for action, args in actions if action == "python" and "install" in args]
            self.assertTrue(any("--force-reinstall" in args for args in installs))
            self.assertFalse((self.root / "_state/deps_incomplete.flag").exists())

    def test_hardware_failure_prevents_large_downloads(self):
        (self.root / "locks").mkdir()
        manifests = {"runtime.json": {"archives": []}, "models.json": {"files": []},
                     "torch-wheels.json": {"wheels": []}, "package-wheels.json": {"wheels": []}}
        for name, content in manifests.items():
            (self.root / "locks" / name).write_text(json.dumps(content), encoding="utf-8")
        with mock.patch.object(self.installer, "_run_python", side_effect=installer_module.InstallError("硬件条件不足")) as run, mock.patch.object(
                self.installer, "prepare_sources") as sources, mock.patch.object(self.installer, "download_models") as models:
            with self.assertRaises(installer_module.InstallError):
                self.installer.run()
        self.assertEqual(run.call_args.args[0][-1], "hardware")
        sources.assert_not_called()
        models.assert_not_called()

    def test_running_services_block_install_without_clearing_cancel(self):
        self.installer.cancel_flag.write_text("pause", encoding="utf-8")
        (self.root / "_state/services.json").write_text('{"comfy":{"pid":10}}', encoding="utf-8")
        with self.assertRaises(installer_module.InstallError):
            self.installer.run(prepare_only=True, resume=True)
        self.assertTrue(self.installer.cancel_flag.exists())

    def test_failed_final_environment_check_never_reports_ready(self):
        (self.root / "locks").mkdir()
        for name, data in {"runtime.json": {"archives": []}, "models.json": {"files": []},
                           "torch-wheels.json": {"wheels": []}, "package-wheels.json": {"wheels": []}}.items():
            (self.root / "locks" / name).write_text(json.dumps(data), encoding="utf-8")
        def environment_step(args, **kwargs):
            if args[-1] == "check":
                raise installer_module.InstallError("源码校验记录缺失")
            return ""
        with mock.patch.object(self.installer, "_run_python", side_effect=environment_step), mock.patch.object(
                self.installer, "prepare_sources"), mock.patch.object(self.installer, "install_dependencies"), mock.patch.object(
                self.installer, "download_models"), mock.patch.object(self.installer, "disk_preflight"):
            with self.assertRaises(installer_module.InstallError):
                self.installer.run()
        self.assertEqual(json.loads(self.installer.status_path.read_text(encoding="utf-8"))["state"], "error")


if __name__ == "__main__":
    unittest.main(verbosity=2)
