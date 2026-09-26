"""
Installation facts collected wherever a device is created.

Rated capacity bounds how much energy a device may claim per interval, and
site coordinates let the anomaly screen know when the sun is down. Both are
optional so existing onboarding keeps working, but a device registered without
a capacity is held to the platform default, which is deliberately loose.
"""

from typing import Optional

from pydantic import BaseModel, Field, model_validator


class SiteSpec(BaseModel):
    rated_capacity_kw: Optional[float] = Field(default=None, gt=0, le=1_000_000)
    latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    longitude: Optional[float] = Field(default=None, ge=-180, le=180)

    @model_validator(mode="after")
    def coordinates_come_in_pairs(self):
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("Provide both latitude and longitude, or neither.")
        return self

    def site_values(self) -> dict:
        return {
            "capacity": self.rated_capacity_kw,
            "latitude": self.latitude,
            "longitude": self.longitude,
        }
