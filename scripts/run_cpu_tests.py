"""发现所有 CPU 单元测试；不加载 torch、不启动真实 Comfy 或模型。"""

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]


def main():
    suites = []
    counts = {}
    # 浏览器服务是导入 ServerTests 的 fixture，单独发现它会重复运行同一批测试。
    fixtures = {"app/test_browser_server.py"}
    for directory in ("scripts", "app"):
        count = 0
        for path in sorted((ROOT / directory).glob("test_*.py")):
            if path.relative_to(ROOT).as_posix() in fixtures:
                continue
            loader = unittest.TestLoader()
            suite = loader.discover(str(ROOT / directory), pattern=path.name)
            count += suite.countTestCases()
            suites.append(suite)
        if not count:
            raise RuntimeError(f"没有发现测试：{directory}/test_*.py")
        counts[directory] = count
    print("CPU test discovery: " + ", ".join(f"{key}={value}" for key, value in counts.items()), flush=True)
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(suites))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
