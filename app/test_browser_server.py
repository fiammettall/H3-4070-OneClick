"""浏览器自测专用临时服务；仅提供模拟队列，不启动 Comfy 或模型。"""

import json
import threading
from pathlib import Path

from test_server import ServerTests, png_bytes


if __name__ == "__main__":
    fixture = ServerTests()
    fixture.setUp()
    fixture.fake.release_prompt.set()
    fixture.fake.complete.set()
    picture = Path(fixture.temporary.name) / "fixture.png"
    picture.write_bytes(png_bytes())
    print("H3_TEST_READY=" + json.dumps({"url": fixture.base, "image": str(picture)}), flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        fixture.tearDown()
