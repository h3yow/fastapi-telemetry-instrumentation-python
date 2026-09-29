"""业务异常类型与统一的失败归类。

三类失败原因彼此可区分（日志、指标 ``outcome``、HTTP 响应共用同一结论）：

* :class:`BusinessError`     —— 业务规则失败（如资源不存在/参数语义非法），
  归类 ``business_error``，映射 4xx。
* :class:`DependencyError`   —— 下游依赖失败（连接/超时/对端错误），
  归类 ``dependency_error``，映射 503。
* 其余未预期 :class:`Exception` —— 归类 ``unexpected_error``，映射 500，
  响应中不含任何内部细节。
"""

from __future__ import annotations


class BusinessError(Exception):
    """可对外说明原因的业务异常。"""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class DependencyError(Exception):
    """调用下游依赖失败；``internal_detail`` 只进日志。"""

    def __init__(self, message: str, internal_detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.internal_detail = internal_detail or message
