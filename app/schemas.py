from datetime import datetime
from uuid import UUID

import pycountry
from pydantic import BaseModel, ConfigDict, Field, field_validator


class CampaignCreate(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=255)
    geo: str
    offer_id: int = Field(gt=0, le=2147483647, strict=True)
    request_id: UUID

    @field_validator('geo')
    @classmethod
    def country_code(cls, value):
        value = value.upper()
        if len(value) != 2 or not pycountry.countries.get(alpha_2=value):
            raise ValueError('Укажите один код страны ISO 3166-1 alpha-2, например MX')
        return value


class OperationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    request_id: str
    name: str
    geo: str
    offer_id: int
    status: str
    alias: str
    campaign_id: int | None
    campaign_url: str | None
    stream_ids: list[int]
    error: str | None
    created_at: datetime
    updated_at: datetime
