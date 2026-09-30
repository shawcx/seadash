"""For running the benchmarks under python3: seadash's @value decorator, doing nothing.
(A benchmark's output mustn't depend on whether its value classes are copied.)"""


def value(cls):
    return cls
