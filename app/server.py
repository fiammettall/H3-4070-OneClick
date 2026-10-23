"""仅在本机运行的 H3 首末帧生成界面；不导入模型或额外 Web 依赖。"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import hmac
import http.server
import json
import mimetypes
import re
import secrets
import socket
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

MAX_UPLOAD = 16 * 1024 * 1024
MAX_JSON = 64 * 1024
ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
SIZE_CHOICES = {(640, 352), (608, 352), (352, 640), (512, 512)}
LENGTH_CHOICES = {56, 124}
ACTIVE_STATES = {"preparing", "uploading", "submitting", "queued", "running", "reconnecting", "canceling"}
TERMINAL_STATES = {"completed", "failed", "canceled"}
APP_DIR = Path(__file__).resolve().parent


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def write_json(path, value):
    # 原子替换可避免断电或窗口关闭时留下半份任务记录。
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)


def json_digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def file_digest(path):
    # 分块读取锁文件与输入，避免大图片产生额外的内存副本。
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_leaf(filename):
    if not isinstance(filename, str) or not filename or len(filename) > 128:
        raise ValueError("文件名为空或过长。")
    if filename in {".", ".."} or any(c in filename for c in '/\\:\x00\r\n'):
        raise ValueError("文件名不能包含路径或控制字符。")
    if any(ord(c) < 32 for c in filename):
        raise ValueError("文件名不能包含控制字符。")
    return filename


def image_dimensions(data):
    """只读取格式头；完整图片解码由 Comfy 执行，避免引入 Pillow。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(data) < 33 or data[8:16] != b"\x00\x00\x00\rIHDR":
            raise ValueError("PNG 文件头不完整。")
        width, height = struct.unpack(">II", data[16:24])
        kind = "png"
    elif data.startswith(b"\xff\xd8"):
        offset, width, height = 2, 0, 0
        while offset < len(data):
            if data[offset] != 0xFF:
                raise ValueError("JPEG 文件头无效。")
            while offset < len(data) and data[offset] == 0xFF:
                offset += 1
            if offset >= len(data):
                break
            marker = data[offset]
            offset += 1
            if marker in {0xD8, 0x01} or 0xD0 <= marker <= 0xD7:
                continue
            if marker in {0xD9, 0xDA} or offset + 2 > len(data):
                break
            length = struct.unpack(">H", data[offset:offset + 2])[0]
            if length < 2 or offset + length > len(data):
                raise ValueError("JPEG 文件头不完整。")
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                if length < 8:
                    raise ValueError("JPEG 尺寸信息无效。")
                height, width = struct.unpack(">HH", data[offset + 3:offset + 7])
                break
            offset += length
        kind = "jpg"
    elif len(data) >= 30 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        if struct.unpack("<I", data[4:8])[0] + 8 != len(data):
            raise ValueError("WebP 文件长度无效。")
        chunk = data[12:16]
        if chunk == b"VP8X":
            # 动图会带入不明确的多帧，首末帧界面只接收静态图。
            if data[20] & 0x02:
                raise ValueError("请使用静态 WebP，或另存为 PNG/JPEG。")
            width = int.from_bytes(data[24:27], "little") + 1
            height = int.from_bytes(data[27:30], "little") + 1
        elif chunk == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
            width, height = struct.unpack("<HH", data[26:30])
            width, height = width & 0x3FFF, height & 0x3FFF
        elif chunk == b"VP8L" and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], "little")
            width, height = (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        else:
            raise ValueError("无法读取此 WebP，请另存为 PNG/JPEG。")
        kind = "webp"
    else:
        raise ValueError("只支持 PNG、JPEG 和静态 WebP 图片。")
    if not (32 <= width <= 8192 and 32 <= height <= 8192) or width * height > 32_000_000:
        raise ValueError("图片每边须在 32～8192 像素之间，且总像素不超过 3200 万。")
    return width, height, kind


def validate_parameters(payload):
    if not isinstance(payload, dict):
        raise ValueError("请求必须是 JSON 对象。")
    first = payload.get("first_image", "")
    last = payload.get("last_image") or None
    if not isinstance(first, str) or not ID_PATTERN.fullmatch(first):
        raise ValueError("请先上传首帧图片。")
    if last is not None and (not isinstance(last, str) or not ID_PATTERN.fullmatch(last)):
        raise ValueError("末帧图片编号无效。")
    prompt = payload.get("prompt", "")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 12000:
        raise ValueError("提示词不能为空，长度不能超过 12000 个字符。")
    width, height, length = payload.get("width", 640), payload.get("height", 352), payload.get("length", 124)
    if type(width) is not int or type(height) is not int or (width, height) not in SIZE_CHOICES:
        raise ValueError("请选择界面提供的尺寸。")
    if type(length) is not int or length not in LENGTH_CHOICES:
        raise ValueError("帧数只能选择 56 或 124。")
    seed = payload.get("seed", "")
    if seed is None or seed == "":
        seed = secrets.randbits(63)
    else:
        # 字符串可避免浏览器把 64 位整数四舍五入。
        if isinstance(seed, bool) or not re.fullmatch(r"\d{1,20}", str(seed)):
            raise ValueError("种子应为非负整数，留空可随机。")
        seed = int(seed)
        if not 0 <= seed <= 2**64 - 1:
            raise ValueError("种子超出 64 位非负整数范围。")
    return {"first_image": first, "last_image": last, "prompt": prompt.strip(), "width": width,
            "height": height, "length": length, "seed": seed, "fps": 24, "steps": 4}


def build_workflow(template, params, first_filename, last_filename=None, job_id=None):
    graph = copy.deepcopy(template)
    graph["20"]["inputs"]["image"] = first_filename
    graph["6"]["inputs"].update({key: params[key] for key in ("prompt", "width", "height", "length")})
    # 简单动作描述自动补成 H3 的结构化格式；完整提示词保持原样。
    if not params["prompt"].lstrip().startswith("integrated_multimodal_description:"):
        reference = "For the target video, at 0.00 seconds, <Picture 1> is fully referenced. "
        if last_filename:
            reference += "At the final frame, <Picture 2> is fully referenced. "
        graph["6"]["inputs"]["prompt"] = (
            "integrated_multimodal_description: " + reference
            + "\n\n[Shot 1] Preserve the character identity, clothing and visual style from the reference image. "
            + params["prompt"] + "\n\noverall_soundscape: N/A\n\nnon_diegetic_music: N/A")
    graph["10"]["inputs"]["noise_seed"] = params["seed"]
    graph["15"]["inputs"]["filename_prefix"] = "H3_4070/" + (job_id or "preview")
    if last_filename:
        graph["21"] = {"class_type": "LoadImage", "inputs": {"image": last_filename}}
        graph["6"]["inputs"]["last_frame"] = ["21", 0]
    else:
        graph.pop("21", None)
        graph["6"]["inputs"].pop("last_frame", None)
    return graph


class NoRedirect(urllib.request.HTTPRedirectHandler):
    # Comfy 只应响应本机，媒体与上传请求不跟随重定向。
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


class ComfyClient:
    def __init__(self, port):
        self.base = f"http://127.0.0.1:{port}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, path, data=None, headers=None, timeout=15):
        return self.opener.open(urllib.request.Request(self.base + path, data=data, headers=headers or {}), timeout=timeout)

    def json(self, path, value=None, timeout=15):
        body = None if value is None else json.dumps(value, ensure_ascii=False).encode("utf-8")
        with self.request(path, body, {"Content-Type": "application/json"} if body is not None else {}, timeout) as response:
            data = response.read(4 * 1024 * 1024)
            # Comfy 的 /queue 和 /interrupt 成功时返回空正文。
            return json.loads(data.decode("utf-8")) if data else {}

    def upload(self, path):
        boundary = "H3UI" + secrets.token_hex(16)
        leaf = safe_leaf(path.name)
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{leaf}\"\r\n"
                "Content-Type: application/octet-stream\r\n\r\n").encode("ascii")
        body += path.read_bytes() + f"\r\n--{boundary}--\r\n".encode("ascii")
        with self.request("/upload/image", body, {"Content-Type": "multipart/form-data; boundary=" + boundary}, 60) as response:
            result = json.loads(response.read(65536).decode("utf-8"))
        name = safe_leaf(result["name"])
        subfolder = result.get("subfolder", "")
        if subfolder:
            if any(part in {"", ".", ".."} for part in subfolder.replace("\\", "/").split("/")) or ":" in subfolder:
                raise ValueError("Comfy 返回了无效的图片目录。")
            return subfolder.replace("\\", "/") + "/" + name
        return name


class App:
    def __init__(self, root, comfy_port, poll_interval=2):
        self.root = Path(root).resolve()
        self.input_dir = self.root / "input"
        self.jobs_dir = self.root / "user" / "jobs"
        self.uploads_dir = self.jobs_dir / "uploads"
        for path in (self.input_dir, self.jobs_dir, self.uploads_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.template = json.loads((self.root / "workflows" / "h3lite_4070_i2v_api.json").read_text(encoding="utf-8"))
        self.comfy = ComfyClient(comfy_port)
        self.comfy_port = comfy_port
        self.poll_interval = poll_interval
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.jobs = {}
        self.stopping = threading.Event()
        self.restore()

    def restore(self):
        for path in self.jobs_dir.glob("*.json"):
            if not ID_PATTERN.fullmatch(path.stem):
                continue
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
                if job.get("id") != path.stem:
                    continue
                self.jobs[path.stem] = job
                if job.get("state") in ACTIVE_STATES or (job.get("state") == "failed" and job.get("retry_blocked") and job.get("prompt_id")):
                    if job.get("prompt_id"):
                        self.update(path.stem, state="reconnecting", message="界面已恢复，正在核对原任务；不会重新提交。")
                        threading.Thread(target=self.monitor, args=(path.stem,), daemon=True).start()
                    else:
                        self.update(path.stem, state="failed", retry_blocked=job.get("state") == "submitting",
                                    message="上次提交阶段中断，未取得可追踪编号。请在 Comfy 高级界面核对队列。" if job.get("state") == "submitting" else "上次界面在登记输入时关闭，尚未提交生成；可以重试。")
            except (OSError, ValueError, KeyError):
                continue

    def update(self, job_id, **updates):
        with self.lock:
            job = self.jobs[job_id]
            job.update(updates)
            job["updated_at"] = now_iso()
            if updates.get("state") in ACTIVE_STATES:
                job.pop("finished_at", None)
                job.pop("elapsed_seconds", None)
            if job.get("state") in TERMINAL_STATES:
                job.setdefault("finished_at", now_iso())
                elapsed_from = job.get("created_at")
                if elapsed_from:
                    job["elapsed_seconds"] = round(max(0, (dt.datetime.fromisoformat(job["finished_at"]) - dt.datetime.fromisoformat(elapsed_from)).total_seconds()), 2)
            write_json(self.jobs_dir / (job_id + ".json"), job)

    def upload(self, filename, data):
        safe_leaf(filename)
        if not data or len(data) > MAX_UPLOAD:
            raise ValueError("单张图片必须小于或等于 16 MB。")
        width, height, kind = image_dimensions(data)
        suffix = Path(filename).suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp"} or (suffix.lstrip(".").replace("jpeg", "jpg") != kind):
            raise ValueError("文件扩展名与图片格式不一致。")
        image_id = secrets.token_hex(16)
        name = image_id + "." + kind
        (self.input_dir / name).write_bytes(data)
        record = {"id": image_id, "filename": name, "original_name": filename, "width": width,
                  "height": height, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "created_at": now_iso()}
        write_json(self.uploads_dir / (image_id + ".json"), record)
        return record

    def uploaded_image(self, image_id):
        if not ID_PATTERN.fullmatch(image_id):
            raise ValueError("图片编号无效。")
        try:
            record = json.loads((self.uploads_dir / (image_id + ".json")).read_text(encoding="utf-8"))
            leaf = safe_leaf(record["filename"])
            path = self.input_dir / leaf
            if path.resolve().parent != self.input_dir.resolve() or not path.is_file():
                raise ValueError("图片文件不存在，请重新上传。")
            return record, path
        except (OSError, KeyError, json.JSONDecodeError) as error:
            raise ValueError("图片记录不存在，请重新上传。") from error

    def create_job(self, payload):
        params = validate_parameters(payload)
        request_id = payload.get("request_id") or secrets.token_hex(16)
        if not isinstance(request_id, str) or not ID_PATTERN.fullmatch(request_id):
            raise ValueError("请求编号无效，请刷新后重试。")
        intent = dict(params)
        intent["seed"] = "" if payload.get("seed") is None or payload.get("seed") == "" else params["seed"]
        fingerprint = json_digest(intent)
        inputs = {}
        for name in ("first_image", "last_image"):
            if params[name]:
                record, path = self.uploaded_image(params[name])
                inputs[name] = {"id": record["id"], "filename": record["filename"], "sha256": file_digest(path)}
        with self.lock:
            previous = next((job for job in self.jobs.values() if job.get("request_id") == request_id), None)
            if previous:
                if previous.get("request_fingerprint") != fingerprint:
                    raise RuntimeError("同一请求编号不能用于不同参数；请重新提交。")
                return self.public_job(previous["id"])
            if any(job.get("state") in ACTIVE_STATES for job in self.jobs.values()):
                raise RuntimeError("已有任务进行中，请等待完成后再提交。")
            job_id = secrets.token_hex(16)
            job = {"id": job_id, "created_at": now_iso(), "updated_at": now_iso(), "state": "preparing",
                   "message": "正在登记输入图片。", "parameters": params, "outputs": [], "inputs": inputs,
                   "request_id": request_id, "request_fingerprint": fingerprint,
                   "component_locks": {path.name: file_digest(path) for path in sorted((self.root / "locks").glob("*.json"))},
                   "template_sha256": json_digest(self.template), "cancel_requested": False,
                   "retry_of": payload.get("retry_of")}
            self.jobs[job_id] = job
            write_json(self.jobs_dir / (job_id + ".json"), job)
        threading.Thread(target=self.submit, args=(job_id,), daemon=True).start()
        return self.public_job(job_id)

    def retry_job(self, job_id, request_id):
        if not isinstance(job_id, str) or not ID_PATTERN.fullmatch(job_id):
            raise ValueError("任务编号无效。")
        with self.lock:
            if job_id not in self.jobs:
                raise ValueError("任务不存在。")
            job = self.jobs[job_id]
            if job.get("state") not in TERMINAL_STATES:
                raise RuntimeError("请等待任务结束，或先取消当前任务。")
            if job.get("retry_blocked"):
                raise RuntimeError("原提交结果尚不确定。请先在 Comfy 高级界面核对，避免重复占用显卡。")
            payload = {**job["parameters"], "request_id": request_id, "retry_of": job_id}
        return self.create_job(payload)

    def cancel_job(self, job_id):
        if not isinstance(job_id, str) or not ID_PATTERN.fullmatch(job_id):
            raise ValueError("任务编号无效。")
        with self.lock:
            if job_id not in self.jobs:
                raise ValueError("任务不存在。")
            if self.jobs[job_id].get("state") not in TERMINAL_STATES:
                self.update(job_id, cancel_requested=True, state="canceling", message="已请求取消，正在核对本任务；不会清空其他任务。")
        return self.public_job(job_id)

    def input_records(self, job_id):
        with self.lock:
            params = self.jobs[job_id]["parameters"]
        records = {}
        for name in ("first_image", "last_image"):
            if params.get(name):
                records[name] = self.uploaded_image(params[name])[0]
        return records

    def stop(self):
        # 只停止本界面的监视线程，不中断任何已提交的 Comfy 任务。
        self.stopping.set()

    def public_job(self, job_id):
        with self.lock:
            job = copy.deepcopy(self.jobs[job_id])
        job.pop("workflow", None)
        job.pop("request_fingerprint", None)
        job["can_retry"] = job.get("state") in TERMINAL_STATES and not job.get("retry_blocked")
        # 保持种子十进制精度，让浏览器可直接复制复现。
        job["parameters"]["seed"] = str(job["parameters"]["seed"])
        for index, output in enumerate(job.get("outputs", [])):
            output["url"] = f"/api/media?job={job_id}&index={index}"
        return job

    def submit(self, job_id):
        submitted = False
        try:
            params = self.jobs[job_id]["parameters"]
            if self.cancel_before_submit(job_id):
                return
            self.update(job_id, state="uploading", message="正在将首末帧登记到本机 Comfy。")
            first = self.comfy.upload(self.uploaded_image(params["first_image"])[1])
            if self.cancel_before_submit(job_id):
                return
            last = self.comfy.upload(self.uploaded_image(params["last_image"])[1]) if params["last_image"] else None
            graph = build_workflow(self.template, params, first, last, job_id)
            if self.cancel_before_submit(job_id):
                return
            # 先持久化确定的 UUID；即便提交响应丢失，重启也能只追踪这一个任务。
            prompt_id = str(uuid.uuid4())
            self.update(job_id, state="submitting", message="正在提交工作流。", workflow=graph,
                        workflow_sha256=json_digest(graph), prompt_id=prompt_id, submission_ambiguous=True)
            submitted = True
            result = self.comfy.json("/prompt", {"prompt": graph, "prompt_id": prompt_id, "client_id": "h3ui-" + job_id}, timeout=60)
            if not result.get("prompt_id"):
                raise ValueError("Comfy 未接受任务：" + json.dumps(result, ensure_ascii=False)[:3000])
            self.update(job_id, state="queued", message="已进入 Comfy 队列。", prompt_id=str(result["prompt_id"]), submission_ambiguous=False)
            self.monitor(job_id)
        except Exception as error:
            ambiguous = submitted and isinstance(error, (TimeoutError, socket.timeout, urllib.error.URLError)) and not isinstance(error, urllib.error.HTTPError)
            if ambiguous:
                self.update(job_id, state="reconnecting", message="提交响应丢失，正在按原任务编号核对；不会重新提交。", retry_blocked=True)
                self.monitor(job_id)
            else:
                self.update(job_id, state="failed", submission_ambiguous=False, message=friendly_error(error))

    def cancel_before_submit(self, job_id):
        if self.jobs[job_id].get("cancel_requested"):
            self.update(job_id, state="canceled", message="已取消，尚未提交生成。")
            return True
        return False

    def monitor(self, job_id):
        started = time.monotonic()
        failures = 0
        missing = 0
        while not self.stopping.is_set() and time.monotonic() - started < 6 * 3600:
            try:
                if self.jobs[job_id].get("state") in TERMINAL_STATES:
                    return
                prompt_id = self.jobs[job_id]["prompt_id"]
                history = self.comfy.json("/history/" + urllib.parse.quote(prompt_id, safe=""))
                item = history.get(prompt_id)
                if item:
                    self.update(job_id, submission_ambiguous=False, retry_blocked=False)
                    status = item.get("status", {})
                    messages = status.get("messages", [])
                    errors = [entry[1] for entry in messages if isinstance(entry, list) and len(entry) > 1 and entry[0] in {"execution_error", "execution_interrupted"}]
                    if status.get("status_str") == "error" or errors:
                        detail = errors[-1] if errors else status
                        if isinstance(detail, dict):
                            detail = str(detail.get("exception_message") or detail.get("message") or detail)
                        interrupted = any(isinstance(entry, list) and entry and entry[0] == "execution_interrupted" for entry in messages)
                        if interrupted and self.jobs[job_id].get("cancel_requested"):
                            self.update(job_id, state="canceled", message="本任务已取消。")
                        else:
                            self.update(job_id, state="failed", message="Comfy 执行失败：" + str(detail)[:3000])
                        return
                    outputs = collect_outputs(item)
                    if status.get("completed") or outputs:
                        if not outputs:
                            self.update(job_id, state="failed", message="Comfy 已结束，但未发现视频输出。请检查 Comfy 日志。")
                        else:
                            self.update(job_id, state="completed", message="视频已生成。请检查人物、动作和首末帧衔接后再验收。", outputs=outputs)
                        return
                queue = self.comfy.json("/queue")
                running_rows = [row for row in queue.get("queue_running", []) if isinstance(row, (list, tuple)) and len(row) > 1]
                running = any(str(row[1]) == prompt_id for row in running_rows)
                pending = [row for row in queue.get("queue_pending", []) if isinstance(row, (list, tuple)) and len(row) > 1]
                position = next((index + 1 for index, row in enumerate(pending) if str(row[1]) == prompt_id), None)
                cancel = self.jobs[job_id].get("cancel_requested")
                if cancel and (running or position is not None):
                    self.update_if_changed(job_id, state="canceling", message="正在取消本任务，等待 Comfy 确认。", submission_ambiguous=False, retry_blocked=False)
                    if position is not None:
                        self.comfy.json("/queue", {"delete": [prompt_id]})
                    elif len(running_rows) == 1 and str(running_rows[0][1]) == prompt_id:
                        # 仅向支持定向中断的锁定版本发出精确编号，竞态中也不能中断外部任务。
                        self.comfy.json("/interrupt", {"prompt_id": prompt_id})
                    else:
                        self.update_if_changed(job_id, message="检测到其他任务同时运行，暂不发出中断；请在高级界面核对。")
                    missing = 0
                elif running:
                    self.update_if_changed(job_id, state="running", message="正在执行 Comfy 工作流；编码、采样和解码阶段可能较久。", submission_ambiguous=False, retry_blocked=False)
                    missing = 0
                elif position is not None:
                    self.update_if_changed(job_id, state="queued", message=f"等待执行，队列位置：{position}。", submission_ambiguous=False, retry_blocked=False)
                    missing = 0
                else:
                    missing += 1
                    if missing >= 15:
                        ambiguous = self.jobs[job_id].get("submission_ambiguous", False)
                        state = "canceled" if cancel and not ambiguous else "failed"
                        self.update(job_id, state=state, retry_blocked=ambiguous,
                                    message="本任务已从队列移除，未生成视频。" if state == "canceled" else "Comfy 队列和历史中均未找到原任务。可能服务已重启；请核对高级界面。" + ("原提交结果不确定，已阻止自动重试。" if ambiguous else "可以复用输入重新生成。"))
                        return
                failures = 0
            except (OSError, ValueError, KeyError, urllib.error.URLError) as error:
                failures += 1
                self.update_if_changed(job_id, state="reconnecting", message="暂时无法连接 Comfy，保留任务编号并等待恢复。" + (" " + friendly_error(error) if failures == 1 else ""))
                if failures >= 150:
                    self.update(job_id, state="failed", retry_blocked=True, message="Comfy 持续无法连接。原任务可能仍在运行，已阻止重复生成；恢复连接后重启界面可核对。")
                    return
            self.stopping.wait(self.poll_interval)
        if not self.stopping.is_set():
            self.update(job_id, state="failed", retry_blocked=True, message="等待超过 6 小时。任务可能仍在 Comfy 中，请先核对再重试。")

    def update_if_changed(self, job_id, **updates):
        with self.lock:
            if all(self.jobs[job_id].get(key) == value for key, value in updates.items()):
                return
            self.update(job_id, **updates)


def collect_outputs(history_item):
    results = []
    # 只展示本工作流保存节点的结果，避免把中间预览当作成片。
    output_node = history_item.get("outputs", {}).get("15", {})
    for key in ("images", "videos", "gifs"):
        for entry in output_node.get(key, []):
            if not isinstance(entry, dict) or entry.get("type", "output") != "output":
                continue
            try:
                filename = safe_leaf(entry["filename"])
                subfolder = entry.get("subfolder", "").replace("\\", "/")
                if subfolder.startswith("/") or ":" in subfolder or any(part in {".", ".."} for part in subfolder.split("/")):
                    continue
                if Path(filename).suffix.lower() not in {".mp4", ".webm", ".mov", ".mkv"}:
                    continue
                record = {"filename": filename, "subfolder": subfolder, "type": "output"}
                if record not in results:
                    results.append(record)
            except (KeyError, ValueError, AttributeError):
                continue
    return results


def friendly_error(error):
    if isinstance(error, urllib.error.HTTPError):
        try:
            content = error.read(8192).decode("utf-8", errors="replace")
            detail = json.loads(content)
            content = json.dumps(detail, ensure_ascii=False)
        except (OSError, ValueError):
            content = str(error)
        return f"Comfy 返回 HTTP {error.code}：{content[:3000]}"
    if isinstance(error, urllib.error.URLError):
        return "无法连接本机 Comfy，请确认启动器已完成启动。" + str(error.reason)[:300]
    return str(error)[:3000]


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def app(self):
        return self.server.app

    def local_request(self):
        expected = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if self.headers.get("Host") not in expected:
            self.send_json(403, {"error": "仅允许本机地址访问。"})
            return False
        return True

    def common_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")

    def send_json(self, status, value):
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.common_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_body(self, maximum):
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("不支持分块上传。")
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError as error:
            raise ValueError("请求长度无效。") from error
        if not 0 <= length <= maximum:
            self.close_connection = True
            raise ValueError("请求内容过大或缺少长度。")
        self.connection.settimeout(60)
        data = self.rfile.read(length)
        if len(data) != length:
            raise ValueError("上传未完成，请重新选择图片。")
        return data

    def do_GET(self):
        if not self.local_request():
            return
        parsed = urllib.parse.urlsplit(self.path)
        try:
            if parsed.path == "/":
                html = (APP_DIR / "index.html").read_text(encoding="utf-8")
                data = html.replace("__LOCAL_TOKEN__", self.app.token).replace("__COMFY_PORT__", str(self.app.comfy_port)).encode("utf-8")
                self.send_response(200)
                self.common_headers()
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' blob:; media-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif parsed.path == "/api/health":
                try:
                    self.app.comfy.json("/queue", timeout=2)
                    online = True
                except (OSError, ValueError):
                    online = False
                self.send_json(200, {"ready": True, "comfy_online": online, "comfy_port": self.app.comfy_port, "validation": "待 RTX 4070 首测"})
            elif parsed.path == "/api/jobs":
                with self.app.lock:
                    ids = sorted(self.app.jobs, key=lambda item: self.app.jobs[item]["created_at"], reverse=True)[:30]
                    jobs = [self.app.public_job(item) for item in ids]
                self.send_json(200, {"jobs": jobs})
            elif parsed.path == "/api/media":
                self.serve_media(urllib.parse.parse_qs(parsed.query))
            elif parsed.path == "/api/job-inputs":
                job_id = urllib.parse.parse_qs(parsed.query).get("job", [""])[0]
                if not ID_PATTERN.fullmatch(job_id) or job_id not in self.app.jobs:
                    raise ValueError("任务编号无效。")
                records = self.app.input_records(job_id)
                for record in records.values():
                    record["url"] = "/api/input?image=" + record["id"]
                self.send_json(200, records)
            elif parsed.path == "/api/input":
                image_id = urllib.parse.parse_qs(parsed.query).get("image", [""])[0]
                record, path = self.app.uploaded_image(image_id)
                data = path.read_bytes()
                self.send_response(200)
                self.common_headers()
                self.send_header("Content-Type", mimetypes.guess_type(record["filename"])[0] or "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif parsed.path == "/favicon.ico":
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self.send_json(404, {"error": "页面不存在。"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (KeyError, ValueError) as error:
            self.send_json(400, {"error": str(error)})
        except (OSError, urllib.error.URLError) as error:
            self.send_json(502, {"error": friendly_error(error)})

    def do_POST(self):
        if not self.local_request():
            return
        if not hmac.compare_digest(self.headers.get("X-Local-Token", ""), self.app.token):
            self.close_connection = True
            self.send_json(403, {"error": "会话已失效，请刷新页面。"})
            return
        parsed = urllib.parse.urlsplit(self.path)
        try:
            if parsed.path == "/api/upload":
                query = urllib.parse.parse_qs(parsed.query)
                result = self.app.upload(query.get("filename", [""])[0], self.read_body(MAX_UPLOAD))
                self.send_json(201, result)
            elif parsed.path in {"/api/generate", "/api/cancel", "/api/retry"}:
                if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                    raise ValueError("生成请求必须是 JSON。")
                payload = json.loads(self.read_body(MAX_JSON).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("请求必须是 JSON 对象。")
                if parsed.path == "/api/cancel":
                    self.send_json(202, self.app.cancel_job(payload.get("job_id")))
                elif parsed.path == "/api/retry":
                    self.send_json(202, self.app.retry_job(payload.get("job_id"), payload.get("request_id")))
                else:
                    self.send_json(202, self.app.create_job(payload))
            else:
                self.close_connection = True
                self.send_json(404, {"error": "接口不存在。"})
        except (ValueError, KeyError, UnicodeError) as error:
            self.send_json(400, {"error": str(error)})
        except RuntimeError as error:
            self.send_json(409, {"error": str(error)})
        except OSError as error:
            self.send_json(500, {"error": "无法写入本机目录：" + str(error)})

    def serve_media(self, query):
        job_id = query.get("job", [""])[0]
        if not ID_PATTERN.fullmatch(job_id) or job_id not in self.app.jobs:
            raise ValueError("任务编号无效。")
        index = int(query.get("index", ["0"])[0])
        outputs = self.app.jobs[job_id].get("outputs", [])
        if not 0 <= index < len(outputs):
            raise ValueError("成片编号无效。")
        output = outputs[index]
        # 代理只接受已记录的成片编号，不提供任意路径读取接口。
        record = collect_outputs({"outputs": {"15": {"images": [output]}}})
        if not record:
            raise ValueError("成片路径无效。")
        headers = {}
        requested_range = self.headers.get("Range")
        if requested_range and re.fullmatch(r"bytes=\d*-\d*", requested_range):
            headers["Range"] = requested_range
        with self.app.comfy.request("/view?" + urllib.parse.urlencode(record[0]), headers=headers, timeout=30) as response:
            self.send_response(response.status)
            self.common_headers()
            self.send_header("Content-Type", mimetypes.guess_type(record[0]["filename"])[0] or "video/mp4")
            for name in ("Content-Length", "Content-Range", "Accept-Ranges"):
                if response.headers.get(name):
                    self.send_header(name, response.headers[name])
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            while True:
                data = response.read(256 * 1024)
                if not data:
                    break
                self.wfile.write(data)

    def log_message(self, format, *args):
        # 只输出请求路径和状态，不记录提示词、上传正文或会话令牌。
        print(f"[{now_iso()}] {format % args}", flush=True)


def create_server(app, port=8195):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.app = app
    return server


def main():
    parser = argparse.ArgumentParser(description="H3 4070 本机首末帧生成界面")
    parser.add_argument("--root", type=Path, default=APP_DIR.parent)
    parser.add_argument("--host", choices=["127.0.0.1"], default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8195)
    parser.add_argument("--comfy-port", type=int, default=8194)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 1 <= args.comfy_port <= 65535 or args.port == args.comfy_port:
        parser.error("端口必须在 1～65535 之间，且界面与 Comfy 端口不能相同。")
    app = App(args.root, args.comfy_port)
    with create_server(app, args.port) as server:
        print(f"H3 4070 界面：http://127.0.0.1:{args.port}/", flush=True)
        print("部署包待 RTX 4070 Laptop 8GB + 32GB 首测；启动界面不会自动生成。", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("界面已停止；已提交的 Comfy 任务可能仍在继续。", flush=True)
        finally:
            app.stop()


if __name__ == "__main__":
    main()
