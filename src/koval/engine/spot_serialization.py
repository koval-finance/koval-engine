"""Serialize Spot controls with engine work and reject mutating callback reentry."""

from functools import wraps


def serialized_spot(*, control=False):
    def decorate(method):
        @wraps(method)
        def call(self, *args, **kwargs):
            with self._spot_lock:
                if control and self._spot_work_depth:
                    raise RuntimeError("reentrant Spot control during engine work")
                self._spot_work_depth += 1
                try:
                    return method(self, *args, **kwargs)
                finally:
                    self._spot_work_depth -= 1

        return call

    return decorate
