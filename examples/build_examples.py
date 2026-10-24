"""用标准库重新生成原创几何参考图、示例配置及 ComfyUI 画布。"""

from __future__ import annotations

import argparse
import binascii
import copy
import hashlib
import json
import math
from pathlib import Path
import struct
import sys
import uuid
import zlib

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from validate_workflows import API_NAME, CANVAS_NAME, instantiate_api, model_downloads, read_json, schema_snapshot


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def scene(raised=False):
    shapes = []

    def add(kind, values, color):
        shapes.append((kind, values, color))

    add("rect", [0, 0, 640, 352], "#dff2fa")
    for x, y, rx, ry in [(105, 58, 42, 16), (145, 53, 36, 20), (518, 62, 42, 17), (554, 56, 28, 19)]:
        add("ellipse", [x, y, rx, ry], "#ffffff")
    add("rect", [40, 110, 560, 133], "#f7f0db")
    add("rect", [34, 100, 572, 12], "#6d9eae")
    for x in range(68, 585, 76):
        for y in (130, 182):
            add("rect", [x, y, 40, 35], "#91c4d5")
            add("rect", [x + 19, y, 2, 35], "#dff2fa")
            add("rect", [x, y + 17, 40, 2], "#dff2fa")
    add("rect", [0, 243, 640, 109], "#ede1c3")
    add("rect", [0, 313, 640, 39], "#b5c8ce")
    for x, y, r in [(40, 229, 26), (75, 224, 30), (110, 232, 25), (540, 227, 27), (578, 226, 31), (615, 230, 24)]:
        add("ellipse", [x, y, r, r], "#86b49a")
    add("ellipse", [319, 303, 58, 8], "#d7c8a8")
    add("rect", [285, 280, 15, 20], "#385b70")
    add("rect", [330, 280, 15, 20], "#385b70")
    add("rect", [278, 296, 29, 10], "#385b70")
    add("rect", [325, 296, 29, 10], "#385b70")
    add("rect", [276, 230, 76, 55], "#579fb0")
    add("rect", [298, 244, 32, 25], "#f6c96d")
    add("line", [276, 241, 252, 260, 12], "#579fb0")
    add("ellipse", [250, 263, 9, 9], "#385b70")
    if raised:
        add("line", [350, 240, 372, 212, 12], "#579fb0")
        add("line", [372, 212, 376, 185, 12], "#579fb0")
        add("ellipse", [377, 180, 9, 9], "#385b70")
    else:
        add("line", [350, 240, 375, 258, 12], "#579fb0")
        add("ellipse", [379, 262, 9, 9], "#385b70")
    add("line", [314, 170, 314, 155, 5], "#385b70")
    add("ellipse", [314, 150, 8, 8], "#f6c96d")
    add("rect", [267, 171, 95, 61], "#385b70")
    add("rect", [275, 180, 79, 43], "#f4f7e8")
    add("ellipse", [295, 198, 5, 6], "#385b70")
    add("ellipse", [334, 198, 5, 6], "#385b70")
    add("line", [308, 213, 322, 213, 3], "#385b70")
    return shapes


def svg_bytes(shapes):
    elements = ['<svg xmlns="http://www.w3.org/2000/svg" width="640" height="352" viewBox="0 0 640 352">']
    for kind, values, color in shapes:
        if kind == "rect":
            x, y, w, h = values
            elements.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{color}"/>')
        elif kind == "ellipse":
            x, y, rx, ry = values
            elements.append(f'<ellipse cx="{x}" cy="{y}" rx="{rx}" ry="{ry}" fill="{color}"/>')
        else:
            x1, y1, x2, y2, width = values
            elements.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="{width}" stroke-linecap="round"/>')
    elements.append("</svg>")
    return ("\n".join(elements) + "\n").encode("utf-8")


def png_bytes(shapes):
    # 相同几何图元用双倍分辨率光栅化，固定像素平均缩小；无需 Pillow 或系统字体。
    scale, width, height = 2, 640, 352
    raster_width = width * scale
    pixels = bytearray(raster_width * height * scale * 3)
    for kind, values, color in shapes:
        rgb = bytes.fromhex(color[1:])
        if kind == "rect":
            x, y, w, h = values
            x1, y1, x2, y2 = x, y, x + w, y + h
        elif kind == "ellipse":
            x, y, rx, ry = values
            x1, y1, x2, y2 = x - rx, y - ry, x + rx, y + ry
        else:
            ax, ay, bx, by, line_width = values
            radius = line_width / 2
            x1, y1, x2, y2 = min(ax, bx) - radius, min(ay, by) - radius, max(ax, bx) + radius, max(ay, by) + radius
        for py in range(max(0, math.floor(y1 * scale)), min(height * scale, math.ceil(y2 * scale))):
            cy = (py + 0.5) / scale
            for px in range(max(0, math.floor(x1 * scale)), min(width * scale, math.ceil(x2 * scale))):
                cx = (px + 0.5) / scale
                if kind == "ellipse" and ((cx - x) / rx) ** 2 + ((cy - y) / ry) ** 2 > 1:
                    continue
                if kind == "line":
                    distance = (bx - ax) ** 2 + (by - ay) ** 2
                    ratio = max(0, min(1, ((cx - ax) * (bx - ax) + (cy - ay) * (by - ay)) / distance))
                    if (cx - ax - ratio * (bx - ax)) ** 2 + (cy - ay - ratio * (by - ay)) ** 2 > radius ** 2:
                        continue
                offset = (py * raster_width + px) * 3
                pixels[offset:offset + 3] = rgb
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        for x in range(width):
            for channel in range(3):
                total = sum(pixels[((y * scale + dy) * raster_width + x * scale + dx) * 3 + channel]
                            for dy in range(scale) for dx in range(scale))
                raw.append(total // (scale * scale))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", binascii.crc32(kind + data) & 0xFFFFFFFF)

    # 手工构造未压缩 DEFLATE 块，避免 CI 环境仅因压缩器版本差异改变资源哈希。
    blocks = []
    for offset in range(0, len(raw), 65535):
        data = bytes(raw[offset:offset + 65535])
        blocks.append(bytes([int(offset + len(data) == len(raw))])
                      + struct.pack("<HH", len(data), len(data) ^ 0xFFFF) + data)
    stream = b"\x78\x01" + b"".join(blocks) + struct.pack(">I", zlib.adler32(raw) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", stream) + chunk(b"IEND", b""))


LAYOUT = {
    "1": ([20, 230], [420, 210], "小编码器与投影 · dynamic"),
    "2": ([20, 20], [420, 130], "W4A8 主模型"),
    "3": ([480, 20], [290, 130], "视频与音频 Sigma Shift"),
    "105:201": ([820, 20], [550, 160], "Turbo LoRA · 保持配套采样参数"),
    "4": ([480, 230], [420, 100], "视频 VAE · FP16"),
    "5": ([950, 230], [420, 100], "音频 VAE · FP32"),
    "20": ([40, 540], [320, 300], "首帧 · 请上传示例 PNG"),
    "21": ([400, 540], [320, 300], "末帧 · 请上传示例 PNG"),
    "6": ([760, 540], [610, 480], "提示词、尺寸与帧数 · 可选末帧"),
    "7": ([1440, 40], [300, 100], "Basic Guider"),
    "8": ([1440, 200], [300, 90], "采样器 · res_multistep"),
    "9": ([1440, 350], [300, 150], "调度器 · simple · 4 个调度步"),
    "10": ([1440, 570], [300, 110], "固定种子 · 对照时保持一致"),
    "11": ([1790, 350], [310, 220], "联合视频与音频采样"),
    "12": ([2200, 40], [270, 100], "解码视频"),
    "13": ([2200, 230], [270, 100], "解码音频"),
    "14": ([2520, 40], [300, 160], "合成视频 · 24 fps · 8 bit"),
    "15": ([2870, 40], [380, 400], "保存与预览 · output/H3_4070"),
}


def canvas_from_api(graph, snapshot, label, root=ROOT):
    # API 子图 ID 含冒号；画布使用唯一整数，并保留显式映射用于逐输入校验。
    ids = {identity: (16 if identity == "105:201" else int(identity)) for identity in graph}
    nodes = []
    by_identity = {}
    for order, (identity, item) in enumerate(graph.items()):
        node_type = item["class_type"]
        schema = snapshot["nodes"][node_type]
        position, size, title = copy.deepcopy(LAYOUT[identity])
        inputs, widgets = [], []
        for slot in schema["inputs"]:
            record = {"name": slot["name"], "type": slot["type"], "link": None}
            if slot.get("widget"):
                record["widget"] = {"name": slot["name"]}
                widgets.append(item["inputs"][slot["name"]])
            inputs.append(record)
        widgets.extend(schema.get("extra_widgets", []))
        node = {"id": ids[identity], "type": node_type, "pos": position, "size": size,
                "flags": {}, "order": order, "mode": 0, "title": title, "inputs": inputs,
                "outputs": [{"name": typ, "type": typ, "slot_index": index, "links": []}
                            for index, typ in enumerate(schema["outputs"])],
                "properties": {"Node name for S&R": node_type, "api_node_id": identity},
                "widgets_values": widgets}
        downloads = model_downloads(node_type, item["inputs"], root)
        if downloads:
            node["properties"]["models"] = downloads
        by_identity[identity] = node
        nodes.append(node)
    links = []
    for identity, item in graph.items():
        node = by_identity[identity]
        for index, slot in enumerate(snapshot["nodes"][item["class_type"]]["inputs"]):
            value = item["inputs"].get(slot["name"])
            if not isinstance(value, list):
                continue
            source, source_slot = value
            link_id = len(links) + 1
            links.append([link_id, ids[source], source_slot, ids[identity], index, slot["type"]])
            node["inputs"][index]["link"] = link_id
            by_identity[source]["outputs"][source_slot]["links"].append(link_id)
    return {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, "h3-4070-oneclick/" + label)), "revision": 1,
            "last_node_id": max(ids.values()), "last_link_id": len(links), "nodes": nodes, "links": links,
            "groups": [{"id": index + 1, "title": title, "bounding": bounds, "color": "#426978",
                        "font_size": 24, "flags": {}}
                       for index, (title, bounds) in enumerate([
                           ("1 · 模型与量化组件（由安装器下载）", [0, -60, 1390, 470]),
                           ("2 · 上传参考图并编辑提示词", [0, 450, 1390, 640]),
                           ("3 · 保持采样器与 Turbo LoRA 配套", [1410, -60, 710, 860]),
                           ("4 · 解码、合成与保存", [2160, -60, 1120, 580])])],
            "config": {}, "extra": {"ds": {"scale": 0.38, "offset": [80, 150]},
                                      "h3_oneclick": {"validation": "static_only_no_gpu", "schema": 1}},
            "version": 0.4}


def products(root=ROOT):
    snapshot = schema_snapshot(root)
    template = read_json(root / API_NAME)
    files = {}
    for name, raised in [("h3_schoolyard_first", False), ("h3_schoolyard_last", True)]:
        drawing = scene(raised)
        files[f"examples/assets/{name}.svg"] = svg_bytes(drawing)
        files[f"examples/assets/{name}.png"] = png_bytes(drawing)
    common = ("A single continuous flat 2D animation shot in the school courtyard shown in <Picture 1>. "
              "Preserve the small teal toy robot, its dark blue rectangular head frame, cream face panel, "
              "two round eyes, yellow antenna and yellow square chest panel. "
              "The robot slowly raises the arm on the viewer's right in a friendly greeting. "
              "Keep its feet planted and the building, windows, shrubs and framing stable. "
              "Fixed camera, soft daylight, clean geometric shapes and a limited pastel palette. No cuts, text or extra characters.")
    configs = []
    for name, frames, last in [("01-smoke-i2v", 56, None), ("02-first-last", 124, "../assets/h3_schoolyard_last.png")]:
        opening = "At 0.00 seconds, <Picture 1> is fully referenced. "
        if last:
            opening += "At the final moment, <Picture 2> is fully referenced. "
        prompt = ("integrated_multimodal_description: " + opening + "\n\n[Shot 1] " + common
                  + (" End in the arm-raised pose of <Picture 2>." if last else "")
                  + "\n\noverall_soundscape: N/A\n\nnon_diegetic_music: N/A")
        config = {"schema": 1, "example": name, "validation": "input_template_no_generated_output",
                  "first_frame": "../assets/h3_schoolyard_first.png", "last_frame": last,
                  "prompt": prompt, "width": 640, "height": 352, "length": frames, "fps": 24,
                  "seed": 40702026, "filename_prefix": "H3_4070/examples/" + name,
                  "sampler": "res_multistep", "scheduler": "simple", "scheduler_steps": 4}
        configs.append(config)
        graph = instantiate_api(template, config)
        files[f"examples/{name}/config.json"] = json_bytes(config)
        files[f"examples/{name}/prompt.txt"] = (prompt + "\n").encode("utf-8")
        files[f"examples/{name}/workflow.api.json"] = json_bytes(graph)
        files[f"examples/{name}/workflow.json"] = json_bytes(canvas_from_api(graph, snapshot, name, root))
    base = copy.deepcopy(template)
    base["6"]["inputs"]["prompt"] = configs[0]["prompt"]
    base["20"]["inputs"]["image"] = "h3_schoolyard_first.png"
    files[CANVAS_NAME] = json_bytes(canvas_from_api(base, snapshot, "baseline-i2v", root))
    files["examples/assets/manifest.json"] = json_bytes({"license": "MIT", "origin": "original_geometry_generated_by_build_examples.py",
        "files": {Path(name).name: {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                  for name, data in files.items() if name.startswith("examples/assets/")}})
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只验证现有资源是否与生成器一致")
    args = parser.parse_args()
    files = products()
    failures = []
    for name, content in files.items():
        path = ROOT / name
        if args.check:
            if not path.is_file() or path.read_bytes() != content:
                failures.append(name)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
    if failures:
        raise SystemExit("示例资源与生成器不一致：" + ", ".join(failures))
    print(json.dumps({"mode": "check" if args.check else "write", "files": len(files), "gpu_used": False}))


if __name__ == "__main__":
    main()
