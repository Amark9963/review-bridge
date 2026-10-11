class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str,
                 headers: dict[str, str] | None = None):
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers


def not_found():
    return ServiceError(404, "not_found", "Resource not found")
