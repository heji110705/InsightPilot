"""测试公共设置。

在**任何测试模块被 import 之前**执行，所以这里能影响 ``app.api.server``
在 import 时构造的模块级限流器。

``IP_RATE_ENABLED=0``：把公开演示的防刷闸门关掉。
理由：整套用例共用一个进程级计数器，默认 3 次/分 会让第 4 条 HTTP 用例开始
全部 429，而那些用例验证的是引擎行为、不是限流。限流自身由
``tests/test_limits.py`` 用一个显式的小实例单独覆盖（它自己 monkeypatch
``server._limiter``，不受这里影响）。
"""
from __future__ import annotations

import os

os.environ.setdefault("IP_RATE_ENABLED", "0")
