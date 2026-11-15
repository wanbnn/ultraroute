class GatewayError(Exception):
    def __init__(self, message, code='provider_error', status=502):
        self.message, self.code, self.status = message, code, status
        super().__init__(message)
