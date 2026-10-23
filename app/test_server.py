"""纯 CPU 自测：只使用临时目录和模拟 Comfy，不访问真实模型服务。"""

import json
import hashlib
import re
import shutil
import struct
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import server


def png_bytes(width=64, height=64):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    rows = (b"\x00" + b"\x80\x90\xa0" * width) * height
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


class FakeComfyHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, code, value, content_type="application/json"):
        data = json.dumps(value).encode("utf-8") if isinstance(value, dict) else value
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if self.path == "/upload/image":
            name = re.search(rb'filename="([a-z0-9.]+)"', body).group(1).decode("ascii")
            self.respond(200, {"name": name, "subfolder": "", "type": "input"})
        elif self.path == "/prompt":
            payload = json.loads(body)
            self.server.graph = payload["prompt"]
            self.server.prompt_id = payload["prompt_id"]
            self.server.prompt_count += 1
            self.server.prompt_started.set()
            self.server.release_prompt.wait(3)
            if self.server.reject_prompt:
                self.respond(400, {"error": {"type": "prompt_outputs_failed_validation"}, "node_errors": {"2": "missing model"}})
            else:
                self.respond(200, {"prompt_id": self.server.prompt_id})
        elif self.path == "/queue":
            payload = json.loads(body)
            self.server.actions.append((self.path, payload))
            if payload == {"delete": [self.server.prompt_id]}:
                self.server.removed = True
            self.respond(200, b"")
        elif self.path == "/interrupt":
            payload = json.loads(body)
            self.server.actions.append((self.path, payload))
            if payload == {"prompt_id": self.server.prompt_id}:
                self.server.interrupted = True
            self.respond(200, b"")
        else:
            self.respond(404, {})

    def do_GET(self):
        if self.path.startswith("/history/"):
            if self.server.interrupted:
                self.respond(200, {self.server.prompt_id: {"status": {"status_str": "error", "messages": [["execution_interrupted", {}]]}, "outputs": {}}})
            elif self.server.fail_generation:
                item = {"status": {"status_str": "error", "messages": [["execution_error", {"exception_message": "模拟显存不足"}]]}, "outputs": {}}
                self.respond(200, {self.server.prompt_id: item})
            elif self.server.complete.is_set():
                self.respond(200, {self.server.prompt_id: {"status": {"completed": True}, "outputs": {"15": {"images": [{"filename": "result.mp4", "subfolder": "H3_4070", "type": "output"}], "animated": [True]}}}})
            else:
                self.respond(200, {})
        elif self.path == "/queue":
            own = [[0, self.server.prompt_id, {}]] if not self.server.removed else []
            mode = self.server.queue_mode
            running = [] if mode in {"pending", "missing"} else own
            if mode in {"pending", "external", "mixed"}:
                running = [[99, "external-prompt", {}]] + (own if mode == "mixed" else [])
            self.respond(200, {"queue_running": running, "queue_pending": own if mode == "pending" else []})
        elif self.path.startswith("/view?"):
            data = b"fake-media"
            if self.headers.get("Range") == "bytes=0-3":
                self.send_response(206)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", "4")
                self.send_header("Content-Range", "bytes 0-3/10")
                self.end_headers()
                self.wfile.write(data[:4])
            else:
                self.respond(200, data, "video/mp4")
        else:
            self.respond(404, {})


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="h3-4070-ui-test-")
        self.root = Path(self.temporary.name)
        (self.root / "workflows").mkdir()
        shutil.copy2(Path(server.__file__).parent.parent / "workflows" / "h3lite_4070_i2v_api.json", self.root / "workflows")
        self.fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeComfyHandler)
        self.fake.prompt_started = threading.Event()
        self.fake.release_prompt = threading.Event()
        self.fake.complete = threading.Event()
        self.fake.reject_prompt = False
        self.fake.fail_generation = False
        self.fake.prompt_id = "not-submitted"
        self.fake.prompt_count = 0
        self.fake.actions = []
        self.fake.queue_mode = "running"
        self.fake.interrupted = False
        self.fake.removed = False
        threading.Thread(target=self.fake.serve_forever, daemon=True).start()
        self.app = server.App(self.root, self.fake.server_port, poll_interval=0.02)
        self.http = server.create_server(self.app, 0)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:" + str(self.http.server_port)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.app.stop()
        self.fake.release_prompt.set()
        self.fake.complete.set()
        for _ in range(100):
            if not any(job["state"] in server.ACTIVE_STATES for job in self.app.jobs.values()):
                break
            time.sleep(0.01)
        self.http.shutdown()
        self.http.server_close()
        self.fake.shutdown()
        self.fake.server_close()
        self.temporary.cleanup()

    def request(self, path, body=None, headers=None, token=True):
        headers = dict(headers or {})
        if token:
            headers["X-Local-Token"] = self.app.token
        if isinstance(body, dict):
            body = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        with self.opener.open(urllib.request.Request(self.base + path, data=body, headers=headers), timeout=3) as response:
            data = response.read()
            return response.status, json.loads(data) if response.headers.get_content_type() == "application/json" else data

    def upload(self):
        return self.request("/api/upload?filename=frame.png", png_bytes())[1]

    def payload(self):
        image = self.upload()
        return {"first_image": image["id"], "prompt": "A girl turns her head.", "seed": "18446744073709551615", "width": 640, "height": 352, "length": 56}

    def await_state(self, job_id, expected):
        for _ in range(200):
            if self.app.jobs[job_id]["state"] == expected:
                return self.app.jobs[job_id]
            time.sleep(0.01)
        self.fail(f"任务未进入状态 {expected}: {self.app.jobs[job_id]}")

    def test_upload_validation_and_path_protection(self):
        record = self.upload()
        self.assertEqual(record["width"], 64)
        self.assertTrue((self.root / "input" / record["filename"]).exists())
        for filename, body in (("..%2Foutside.png", png_bytes()), ("frame.jpg", png_bytes()), ("frame.png", b"not an image")):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.request("/api/upload?filename=" + filename, body)
            self.assertEqual(caught.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request("/api/upload?filename=frame.png", png_bytes(), token=False)
        self.assertEqual(caught.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request("/api/health", headers={"Host": "evil.example"})
        self.assertEqual(caught.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request("/app/server.py")
        self.assertEqual(caught.exception.code, 404)

    def test_generation_is_async_records_parameters_and_proxies_result(self):
        payload = self.payload()
        last = self.upload()
        payload["last_image"] = last["id"]
        status, job = self.request("/api/generate", payload)
        self.assertEqual(status, 202)
        self.assertTrue(self.fake.prompt_started.wait(1))
        # 模拟 Comfy 提交接口仍在等待时，状态页依然可访问。
        self.assertEqual(self.request("/api/jobs")[0], 200)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request("/api/generate", payload)
        self.assertEqual(caught.exception.code, 409)
        self.fake.release_prompt.set()
        self.await_state(job["id"], "running")
        self.fake.complete.set()
        result = self.await_state(job["id"], "completed")
        self.assertEqual(result["parameters"]["seed"], 2**64 - 1)
        self.assertEqual(self.fake.graph["6"]["inputs"]["last_frame"], ["21", 0])
        self.assertEqual(self.fake.graph["9"]["inputs"]["steps"], 4)
        self.assertEqual(self.fake.graph["1"]["inputs"]["mode"], "dynamic")
        self.assertEqual(self.fake.graph["8"]["inputs"]["sampler_name"], "res_multistep")
        self.assertEqual(self.fake.graph["2"]["inputs"]["unet_name"], "minimax_h3_fl2va_pruned_w4a8_mixed.safetensors")
        public = self.request("/api/jobs")[1]["jobs"][0]
        self.assertEqual(public["parameters"]["seed"], str(2**64 - 1))
        self.assertEqual(self.request(public["outputs"][0]["url"])[1], b"fake-media")
        self.assertEqual(self.request(public["outputs"][0]["url"], headers={"Range": "bytes=0-3"}), (206, b"fake"))
        record = json.loads((self.root / "user" / "jobs" / (job["id"] + ".json")).read_text(encoding="utf-8"))
        self.assertIn("workflow", record)
        self.assertEqual(record["state"], "completed")
        self.assertEqual(record["workflow_sha256"], server.json_digest(record["workflow"]))
        self.assertEqual(record["inputs"]["first_image"]["sha256"], hashlib.sha256(png_bytes()).hexdigest())
        self.assertIn("component_locks", record)
        self.assertGreaterEqual(record["elapsed_seconds"], 0)

    def test_upstream_validation_error_is_visible(self):
        self.fake.reject_prompt = True
        self.fake.release_prompt.set()
        job = self.request("/api/generate", self.payload())[1]
        result = self.await_state(job["id"], "failed")
        self.assertIn("HTTP 400", result["message"])
        self.assertIn("missing model", result["message"])

    def test_generation_error_is_visible(self):
        self.fake.fail_generation = True
        self.fake.release_prompt.set()
        job = self.request("/api/generate", self.payload())[1]
        result = self.await_state(job["id"], "failed")
        self.assertIn("模拟显存不足", result["message"])

    def test_request_id_is_idempotent_before_and_after_completion(self):
        payload = {**self.payload(), "seed": "", "request_id": "a" * 32}
        first = self.request("/api/generate", payload)[1]
        self.assertTrue(self.fake.prompt_started.wait(1))
        second = self.request("/api/generate", payload)[1]
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["parameters"]["seed"], second["parameters"]["seed"])
        self.assertEqual(self.fake.prompt_count, 1)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request("/api/generate", {**payload, "prompt": "A different action."})
        self.assertEqual(caught.exception.code, 409)
        self.fake.release_prompt.set()
        self.fake.complete.set()
        self.await_state(first["id"], "completed")
        self.assertEqual(self.request("/api/generate", payload)[1]["id"], first["id"])
        self.assertEqual(self.fake.prompt_count, 1)

    def test_cancel_pending_deletes_only_owned_prompt(self):
        self.fake.queue_mode = "pending"
        self.fake.release_prompt.set()
        job = self.request("/api/generate", self.payload())[1]
        self.assertTrue(self.fake.prompt_started.wait(1))
        self.request("/api/cancel", {"job_id": job["id"]})
        self.await_state(job["id"], "canceled")
        self.assertEqual(self.fake.actions, [("/queue", {"delete": [self.fake.prompt_id]})])

    def test_cancel_running_targets_exact_owned_prompt(self):
        self.fake.release_prompt.set()
        job = self.request("/api/generate", self.payload())[1]
        self.await_state(job["id"], "running")
        self.request("/api/cancel", {"job_id": job["id"]})
        self.await_state(job["id"], "canceled")
        self.assertEqual(self.fake.actions, [("/interrupt", {"prompt_id": self.fake.prompt_id})])

    def test_cancel_never_interrupts_external_or_mixed_running(self):
        self.fake.queue_mode = "mixed"
        self.fake.release_prompt.set()
        job = self.request("/api/generate", self.payload())[1]
        self.await_state(job["id"], "running")
        self.request("/api/cancel", {"job_id": job["id"]})
        time.sleep(0.12)
        self.assertEqual(self.fake.actions, [])
        self.assertIn("其他任务", self.app.jobs[job["id"]]["message"])
        self.fake.queue_mode = "external"
        self.await_state(job["id"], "canceled")
        self.assertEqual(self.fake.actions, [])

    def test_cancel_during_submit_is_honored_after_acceptance(self):
        job = self.request("/api/generate", self.payload())[1]
        self.assertTrue(self.fake.prompt_started.wait(1))
        self.request("/api/cancel", {"job_id": job["id"]})
        self.assertEqual(self.fake.actions, [])
        self.fake.release_prompt.set()
        self.await_state(job["id"], "canceled")
        self.assertEqual(self.fake.prompt_count, 1)

    def test_restart_recovers_history_and_reuses_inputs(self):
        self.fake.release_prompt.set()
        original = self.request("/api/generate", self.payload())[1]
        self.await_state(original["id"], "running")
        self.app.stop()
        self.fake.complete.set()
        restarted = server.App(self.root, self.fake.server_port, poll_interval=0.02)
        self.http.app = restarted
        self.app = restarted
        self.await_state(original["id"], "completed")
        inputs = self.request("/api/job-inputs?job=" + original["id"])[1]
        self.assertEqual(self.request(inputs["first_image"]["url"])[1], png_bytes())
        self.assertEqual(self.fake.prompt_count, 1)
        retry = self.request("/api/retry", {"job_id": original["id"], "request_id": "b" * 32})[1]
        self.assertNotEqual(retry["id"], original["id"])
        result = self.await_state(retry["id"], "completed")
        self.assertEqual(result["retry_of"], original["id"])
        self.assertEqual(result["parameters"], restarted.jobs[original["id"]]["parameters"])
        self.assertEqual(self.request("/api/retry", {"job_id": original["id"], "request_id": "b" * 32})[1]["id"], retry["id"])
        self.assertEqual(self.fake.prompt_count, 2)

    def test_restart_missing_prompt_fails_instead_of_running_forever(self):
        self.fake.queue_mode = "missing"
        job_id = "c" * 32
        server.write_json(self.root / "user" / "jobs" / (job_id + ".json"), {
            "id": job_id, "created_at": server.now_iso(), "state": "running", "prompt_id": "lost-prompt",
            "parameters": server.validate_parameters(self.payload()), "outputs": []})
        self.app.stop()
        self.app = server.App(self.root, self.fake.server_port, poll_interval=0.02)
        self.http.app = self.app
        result = self.await_state(job_id, "failed")
        self.assertIn("未找到原任务", result["message"])
        self.assertFalse(result["retry_blocked"])

    def test_ambiguous_submission_blocks_retry(self):
        self.fake.release_prompt.set()
        self.fake.complete.set()
        job = self.request("/api/generate", self.payload())[1]
        self.await_state(job["id"], "completed")
        self.app.update(job["id"], state="failed", retry_blocked=True)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request("/api/retry", {"job_id": job["id"], "request_id": "d" * 32})
        self.assertEqual(caught.exception.code, 409)
        self.assertIn("原提交结果", caught.exception.read().decode("utf-8"))

    def test_parameters_and_output_paths(self):
        payload = self.payload()
        for updates in ({"seed": []}, {"seed": -1}, {"seed": str(2**64)}, {"width": 4096}, {"length": 300}, {"prompt": " "}, {"first_image": "../../x"}):
            with self.assertRaises(ValueError):
                server.validate_parameters({**payload, **updates})
        graph = server.build_workflow(self.app.template, server.validate_parameters(payload), "first.png")
        self.assertNotIn("last_frame", graph["6"]["inputs"])
        self.assertNotIn("21", graph)
        bad = {"outputs": {"15": {"images": [{"filename": "result.mp4", "subfolder": "../../", "type": "output"}, {"filename": "../file.mp4", "type": "output"}]}}}
        self.assertEqual(server.collect_outputs(bad), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
