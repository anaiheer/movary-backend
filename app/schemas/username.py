import re

from pydantic_core import PydanticCustomError


USERNAME_PATTERN = re.compile(r"^[\w.-]+$", re.UNICODE)


def validate_username(value: str) -> str:
    normalized = str(value or "").strip()

    if len(normalized) < 2:
        raise PydanticCustomError("username_too_short", "用户名至少需要 2 个字符")
    if len(normalized) > 64:
        raise PydanticCustomError("username_too_long", "用户名不能超过 64 个字符")
    if not USERNAME_PATTERN.fullmatch(normalized):
        raise PydanticCustomError(
            "username_invalid", "用户名仅支持中文、字母、数字、下划线、中划线和点"
        )

    return normalized


def normalize_login_identifier(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise PydanticCustomError("login_identifier_required", "请输入用户名或邮箱")
    return normalized
