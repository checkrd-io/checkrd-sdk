from enum import Enum


class DeviceTokenResponseType1Status(str, Enum):
    SLOW_DOWN = "slow_down"

    def __str__(self) -> str:
        return str(self.value)
