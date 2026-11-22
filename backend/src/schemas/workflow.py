from typing import Any

from pydantic import BaseModel, Field


class LoginForm(BaseModel):
    username: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=128)


class CaseForm(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    payload: dict[str, Any]


class IntakeForm(BaseModel):
    source: str = Field(default='legacy', max_length=10)
    receipt_key: str | None = Field(default=None, max_length=80)
    title: str = Field(min_length=1, max_length=120)
    payload: dict[str, Any]


class StorageSwitchForm(BaseModel):
    mode: str = Field(min_length=1, max_length=10)


class ReviewForm(BaseModel):
    note: str = Field(min_length=1, max_length=500)
