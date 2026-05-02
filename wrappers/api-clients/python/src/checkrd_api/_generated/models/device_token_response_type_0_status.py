from enum import Enum


class DeviceTokenResponseType0Status(str, Enum):
    PENDING = "pending"

    def __str__(self) -> str:
        return str(self.value)
