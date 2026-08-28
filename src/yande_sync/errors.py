class YandeSyncError(Exception):
    exit_code = 1


class UserError(YandeSyncError, ValueError):
    exit_code = 2


class OperationalError(YandeSyncError):
    exit_code = 1


class RemoteDataError(OperationalError):
    pass
