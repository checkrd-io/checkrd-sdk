from enum import Enum


class DeviceTokenResponseType2Status(str, Enum):
    DENIED = "denied"

    def __str__(self) -> str:
        return str(self.value)
