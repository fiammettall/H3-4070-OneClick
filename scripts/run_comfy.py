from pathlib import Path
import json
import os
import runpy
import sys


ROOT = Path(__file__).resolve().parent.parent
config = json.loads((ROOT / "locks/runtime.json").read_text(encoding="utf-8"))
engine = ROOT / "engine"
sys.path.insert(0, str(engine))
os.chdir(engine)
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ["DO_NOT_TRACK"] = "1"
sys.argv = [str(engine / "main.py"), "--base-directory", str(ROOT),
            "--listen", "127.0.0.1", "--port", str(config["ports"]["comfy"]),
            "--cache-none", "--reserve-vram", "1", "--disable-auto-launch",
            "--disable-api-nodes"]
runpy.run_path(str(engine / "main.py"), run_name="__main__")
