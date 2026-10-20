"""Request bodies (responses are plain dicts built in the routes)."""
import re

from pydantic import BaseModel, Field, StrictBool, field_validator

from .validation import clean_text

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class RegisterIn(BaseModel):
    full_name: str = Field(min_length=1, max_length=120)
    email: str = Field(max_length=254)
    password: str = Field(min_length=8, max_length=200)
    accept_terms: StrictBool

    @field_validator("full_name")
    @classmethod
    def _name(cls, v: str) -> str:
        return clean_text(v)

    @field_validator("accept_terms")
    @classmethod
    def _terms(cls, v: bool) -> bool:
        if not v:
            raise ValueError("the terms must be accepted")
        return v

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip().lower()
        if not _EMAIL.match(v):
            raise ValueError("not a valid email address")
        return v


class EmailIn(BaseModel):
    email: str = Field(max_length=254)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return v.strip().lower()


class VerifyIn(BaseModel):
    email: str = Field(max_length=254)
    code: str = Field(max_length=12, pattern=r"^\s*\d{3}\s?\d{3}\s*$")

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return v.strip().lower()

    @field_validator("code")
    @classmethod
    def _code(cls, v: str) -> str:
        return v.replace(" ", "").strip()


class ChangePasswordIn(BaseModel):
    current_password: str = Field(max_length=200)
    new_password: str = Field(min_length=8, max_length=200)


class GoogleIn(BaseModel):
    id_token: str = Field(max_length=4000)
    accept_terms: StrictBool = False                     # needed the first time (when the account is created)


class LoginIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return v.strip().lower()


class NameIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def _clean(cls, v: str) -> str:
        return clean_text(v)


class SessionIn(BaseModel):
    title: str = Field(min_length=1, max_length=160)

    @field_validator("title")
    @classmethod
    def _clean(cls, v: str) -> str:
        return clean_text(v)


class AttemptIn(BaseModel):
    answers: list[int | None] = Field(max_length=100)
