class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message


def not_found():
    return ServiceError(404, "not_found", "Resource not found")
