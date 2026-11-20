DEFAULT_POLL_SECONDS = 15


def positive_int(value) -> int:
    parsed = int(value)
    if parsed < 1:
        raise ValueError("must be a positive integer")
    return parsed
