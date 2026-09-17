"""Provider account failures are operational errors, not defective translations."""


def is_balance_error(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    message = str(exc).lower()
    return status == 402 or "insufficient balance" in message or "余额不足" in message


class ProviderAccountUnavailable(RuntimeError):
    def __init__(self, provider: str, reason: str = "insufficient_balance"):
        self.provider = provider
        self.reason = reason
        detail = "模型服务余额不足" if reason == "insufficient_balance" else "模型服务鉴权或权限失败"
        super().__init__(f"翻译暂停：{detail}（{provider}）。已完成译文缓存已保留；恢复服务后可复用缓存继续。")
