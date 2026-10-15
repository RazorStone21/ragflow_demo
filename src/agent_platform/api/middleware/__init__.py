"""HTTP 中间件与统一异常处理（CLAUDE.md 2.2 / 8.2）。

两个模块各自负责一半：

- ``request_context``：request_id 生成与回传、结构化访问日志；
- ``errors``：把异常层级映射为 RFC 9457 ``application/problem+json`` 响应。

异常处理取 FastAPI 的 ``add_exception_handler`` 注册式（按异常类 ``__mro__`` 精确匹配），
而非中间件里 ``try/except`` 兜底——后者会连 SSE 之类已发出响应头的场景一起吞掉。
"""
