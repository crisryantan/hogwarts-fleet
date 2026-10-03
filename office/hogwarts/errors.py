from __future__ import annotations


class StoreError(Exception):
    exit_code = 1


class ValidationError(StoreError):
    exit_code = 2


class ConflictError(StoreError):
    exit_code = 3


class NotFoundError(StoreError):
    exit_code = 4


class IntegrityError(StoreError):
    exit_code = 5


class TokenError(StoreError):
    exit_code = 6
