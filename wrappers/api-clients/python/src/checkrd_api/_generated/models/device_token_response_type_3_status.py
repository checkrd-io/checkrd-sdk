from enum import Enum


class DeviceTokenResponseType3Status(str, Enum):
    EXPIRED = "expired"

    def __str__(self) -> str:
        return str(self.value)
