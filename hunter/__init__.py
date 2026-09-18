"""iphone-hunter —— 盯 Apple 官网库存，新机开卖第一时间叫醒你。"""

import os

__version__ = "1.0.0"

#: 提示信息里该写哪条命令。Windows 的 venv 把可执行文件放在 Scripts\ 而不是
#: bin/，照抄 Linux 的写法过去就是「系统找不到指定的路径」。
PY_CMD = r".venv\Scripts\python" if os.name == "nt" else ".venv/bin/python"
