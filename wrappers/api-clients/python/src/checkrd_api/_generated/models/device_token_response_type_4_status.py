from enum import Enum


class DeviceTokenResponseType4Status(str, Enum):
    APPROVED = "approved"

    def __str__(self) -> str:
        return str(self.value)
