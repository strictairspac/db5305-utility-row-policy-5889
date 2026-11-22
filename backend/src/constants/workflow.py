from enum import StrEnum


class Role(StrEnum):
    OPERATOR = 'operator'
    REVIEWER = 'reviewer'
    VIEWER = 'viewer'


class Status(StrEnum):
    QUEUED = 'queued'
    RUNNING = 'running'
    COMPLETE = 'complete'
    FAILED = 'failed'


class StorageVersion(StrEnum):
    """How a case archive is materialised.

    V1 keeps the whole payload as one JSON document (整批存放).
    V2 stores every work order as its own row (逐单入库).
    """

    V1 = 'v1'
    V2 = 'v2'


class StorageMode(StrEnum):
    """Which intake side is allowed to be authoritative.

    LEGACY  old side only; the new side writes through as V1.
    DUAL    both sides accept and both representations are written; the old
            side's receipt is authoritative until an explicit cutover.
    V2      per-order storage only; the new side's receipt is authoritative.
    """

    LEGACY = 'legacy'
    DUAL = 'dual'
    V2 = 'v2'
