ACCOUNT_PASSWORD_RULE = "密码至少 8 位，且需同时包含字母和数字"
ACCOUNT_PASSWORD_MAX_LENGTH = 64


def validate_account_password(value: str, *, field_name: str = "密码") -> str:
    password = value or ""

    if len(password) > ACCOUNT_PASSWORD_MAX_LENGTH:
        raise ValueError(f"{field_name}不能超过 {ACCOUNT_PASSWORD_MAX_LENGTH} 位")

    has_letter = any(char.isalpha() for char in password)
    has_digit = any(char.isdigit() for char in password)
    if len(password) < 8 or not has_letter or not has_digit:
        raise ValueError(
            ACCOUNT_PASSWORD_RULE
            if field_name == "密码"
            else f"{field_name}至少 8 位，且需同时包含字母和数字"
        )

    return value
