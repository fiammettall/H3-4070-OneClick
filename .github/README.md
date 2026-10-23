GitHub 自动检查在 push、pull request 或手动运行时触发。提交仓库后需要查看真实 Actions 结果；本地脚本通过不等于远程任务已通过。

`validate.yml` 包含两个 Windows CPU 任务：

- 版本锁、依赖 wheel 覆盖、模型清单、Actions 完整 commit、源码 UTF-8 检查。
- 可导入画布与 API 工作流、示例输入一致性检查。
- Windows PowerShell 5.1 语法、bootstrap UTF-8 BOM 和 6 个 CMD 入口检查。
- 动态发现 scripts、app 目录里的 `test_*.py`；浏览器临时服务 fixture 排除，避免重复运行它导入的测试。
- 构建并校验 ZIP，上传保存 7 天的预览包。
- 使用固定 Playwright 1.56.1 和模拟 Comfy 服务，检查上传、提交、历史恢复、重试、64 位种子和手机布局，并保存截图。

CPU 任务只安装 `.github/requirements-ci.txt` 中的小型依赖。浏览器工具按 npm lockfile 安装到 runner 临时目录，不进入发行包。CI 不安装 Torch、不下载模型、不启动真实 Comfy，也不代表 4070 实机验收完成。

本地等价检查（先进入项目根目录）：

```powershell
python -m pip install -r .github/requirements-ci.txt
python scripts/check_project.py
python scripts/validate_workflows.py
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/check_windows.ps1
python scripts/run_cpu_tests.py
python scripts/build_release.py
```

Actions 引用来源保存在 `action-pins.json`。更新 Action 时，先核对官方仓库 tag 指向的 commit，再同时更新 workflow 和该清单。更新 Playwright 时，使用官方 npm registry 重新生成 `browser/package-lock.json`，并重新运行浏览器回归。
