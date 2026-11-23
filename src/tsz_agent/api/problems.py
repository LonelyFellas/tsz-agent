class Problem(Exception):
    def __init__(self, status: int, code: str):
        self.status, self.code = status, code
