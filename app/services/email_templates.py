import re
from copy import deepcopy
from enum import Enum
from html import escape
from typing import Any, Mapping

from app.core.public_urls import build_site_url, get_site_base_url
from app.services.site_languages import SUPPORTED_SITE_LANGUAGES, normalize_site_language


class EmailTemplateKey(str, Enum):
    EMAIL_VERIFICATION = "email_verification"
    PASSWORD_CHANGED = "password_changed"
    INVITATION = "invitation"
    SMTP_TEST = "smtp_test"


EMAIL_TEMPLATE_DEFINITIONS: list[dict[str, Any]] = [
    {
        "key": EmailTemplateKey.EMAIL_VERIFICATION.value,
        "label": "邮箱验证",
        "description": "用于注册、换绑邮箱后的验证通知。",
        "variables": [
            "site_name",
            "site_url",
            "site_logo_url",
            "site_logo",
            "username",
            "verify_url",
        ],
        "subject": "请验证您的邮箱",
        "text_body": (
            "您好 {{username}}，\n\n"
            "请点击以下链接完成邮箱验证：\n"
            "{{verify_url}}\n\n"
            "如果不是您本人操作，请忽略此邮件。\n\n"
            "{{site_name}}"
        ),
        "html_body": (
            "<div style='font-family:Arial,sans-serif;line-height:1.7;color:#0f172a'>"
            "{{site_logo}}"
            "<p>您好 <strong>{{username}}</strong>，</p>"
            "<p>请点击以下链接完成邮箱验证：</p>"
            "<p><a href='{{verify_url}}'>{{verify_url}}</a></p>"
            "<p>如果不是您本人操作，请忽略此邮件。</p>"
            "<p style='margin-top:24px'>{{site_name}}</p>"
            "</div>"
        ),
        "i18n": {
            "en-US": {
                "label": "Email Verification",
                "description": "Used for verification emails during registration or when changing the bound email address.",
                "subject": "Please verify your email",
                "text_body": (
                    "Hello {{username}},\n\n"
                    "Please click the link below to complete email verification:\n"
                    "{{verify_url}}\n\n"
                    "If this was not you, please ignore this email.\n\n"
                    "{{site_name}}"
                ),
                "html_body": (
                    "<div style='font-family:Arial,sans-serif;line-height:1.7;color:#0f172a'>"
                    "{{site_logo}}"
                    "<p>Hello <strong>{{username}}</strong>,</p>"
                    "<p>Please click the link below to complete email verification:</p>"
                    "<p><a href='{{verify_url}}'>{{verify_url}}</a></p>"
                    "<p>If this was not you, please ignore this email.</p>"
                    "<p style='margin-top:24px'>{{site_name}}</p>"
                    "</div>"
                ),
            }
        },
    },
    {
        "key": EmailTemplateKey.PASSWORD_CHANGED.value,
        "label": "密码修改通知",
        "description": "用于提醒用户账户登录密码已被修改。",
        "variables": [
            "site_name",
            "site_url",
            "site_logo_url",
            "site_logo",
            "username",
            "changed_at",
        ],
        "subject": "您的密码已修改",
        "text_body": (
            "您好 {{username}}，\n\n"
            "您的账户密码已于 {{changed_at}} 完成修改。\n"
            "如果不是您本人操作，请尽快登录并修改密码。\n\n"
            "{{site_name}}"
        ),
        "html_body": (
            "<div style='font-family:Arial,sans-serif;line-height:1.7;color:#0f172a'>"
            "{{site_logo}}"
            "<p>您好 <strong>{{username}}</strong>，</p>"
            "<p>您的账户密码已于 <strong>{{changed_at}}</strong> 完成修改。</p>"
            "<p>如果不是您本人操作，请尽快登录并修改密码。</p>"
            "<p style='margin-top:24px'>{{site_name}}</p>"
            "</div>"
        ),
        "i18n": {
            "en-US": {
                "label": "Password Change Notification",
                "description": "Used to notify users that their account login password has been changed.",
                "subject": "Your password has been changed",
                "text_body": (
                    "Hello {{username}},\n\n"
                    "Your account password was changed at {{changed_at}}.\n"
                    "If this was not you, please sign in and change your password immediately.\n\n"
                    "{{site_name}}"
                ),
                "html_body": (
                    "<div style='font-family:Arial,sans-serif;line-height:1.7;color:#0f172a'>"
                    "{{site_logo}}"
                    "<p>Hello <strong>{{username}}</strong>,</p>"
                    "<p>Your account password was changed at <strong>{{changed_at}}</strong>.</p>"
                    "<p>If this was not you, please sign in and change your password immediately.</p>"
                    "<p style='margin-top:24px'>{{site_name}}</p>"
                    "</div>"
                ),
            }
        },
        "legacy_i18n": {
            "zh-CN": {
                "html_body": (
                    "<div style='font-family:Arial,sans-serif;line-height:1.7;color:#0f172a'>"
                    "<p>您好 <strong>{{username}}</strong>，</p>"
                    "<p>您的账户密码已于 <strong>{{changed_at}}</strong> 完成修改。</p>"
                    "<p>如果不是您本人操作，请尽快登录并修改密码。</p>"
                    "<p style='margin-top:24px'>{{site_name}}</p>"
                    "</div>"
                )
            },
            "en-US": {
                "html_body": (
                    "<div style='font-family:Arial,sans-serif;line-height:1.7;color:#0f172a'>"
                    "<p>Hello <strong>{{username}}</strong>,</p>"
                    "<p>Your account password was changed at <strong>{{changed_at}}</strong>.</p>"
                    "<p>If this was not you, please sign in and change your password immediately.</p>"
                    "<p style='margin-top:24px'>{{site_name}}</p>"
                    "</div>"
                )
            },
        },
    },
    {
        "key": EmailTemplateKey.INVITATION.value,
        "label": "邀请用户",
        "description": "用于向受邀邮箱发送注册链接和邀请码。",
        "variables": [
            "site_name",
            "site_url",
            "site_logo_url",
            "site_logo",
            "inviter_username",
            "invitee_email",
            "invite_url",
            "invite_code",
            "expires_at",
        ],
        "subject": "{{inviter_username}} 邀请您加入 {{site_name}}",
        "text_body": (
            "您好，\n\n"
            "{{inviter_username}} 邀请您加入 {{site_name}}。\n"
            "注册链接：{{invite_url}}\n"
            "邀请码：{{invite_code}}\n"
            "有效期至：{{expires_at}}\n\n"
            "如果您并未期待此邀请，请忽略此邮件。\n\n"
            "{{site_name}}"
        ),
        "html_body": (
            "<div style='font-family:Arial,sans-serif;line-height:1.7;color:#0f172a'>"
            "{{site_logo}}"
            "<p>您好，</p>"
            "<p><strong>{{inviter_username}}</strong> 邀请您加入 <strong>{{site_name}}</strong>。</p>"
            "<p>点击下面的链接即可开始注册：</p>"
            "<p><a href='{{invite_url}}'>{{invite_url}}</a></p>"
            "<p>邀请码：<strong>{{invite_code}}</strong></p>"
            "<p>有效期至：{{expires_at}}</p>"
            "<p>如果您并未期待此邀请，请忽略此邮件。</p>"
            "<p style='margin-top:24px'>{{site_name}}</p>"
            "</div>"
        ),
        "i18n": {
            "en-US": {
                "label": "Invite User",
                "description": "Used to send a registration link and invitation code to an invited email address.",
                "subject": "{{inviter_username}} invited you to join {{site_name}}",
                "text_body": (
                    "Hello,\n\n"
                    "{{inviter_username}} invited you to join {{site_name}}.\n"
                    "Registration link: {{invite_url}}\n"
                    "Invitation code: {{invite_code}}\n"
                    "Valid until: {{expires_at}}\n\n"
                    "If you were not expecting this invitation, please ignore this email.\n\n"
                    "{{site_name}}"
                ),
                "html_body": (
                    "<div style='font-family:Arial,sans-serif;line-height:1.7;color:#0f172a'>"
                    "{{site_logo}}"
                    "<p>Hello,</p>"
                    "<p><strong>{{inviter_username}}</strong> invited you to join <strong>{{site_name}}</strong>.</p>"
                    "<p>Click the link below to start registration:</p>"
                    "<p><a href='{{invite_url}}'>{{invite_url}}</a></p>"
                    "<p>Invitation code: <strong>{{invite_code}}</strong></p>"
                    "<p>Valid until: {{expires_at}}</p>"
                    "<p>If you were not expecting this invitation, please ignore this email.</p>"
                    "<p style='margin-top:24px'>{{site_name}}</p>"
                    "</div>"
                ),
            }
        },
    },
    {
        "key": EmailTemplateKey.SMTP_TEST.value,
        "label": "SMTP 测试",
        "description": "用于验证当前 SMTP 配置是否可用。",
        "variables": ["site_name", "site_url", "site_logo_url", "site_logo", "to_email", "sent_at"],
        "subject": "SMTP 测试邮件",
        "text_body": (
            "这是一封来自 {{site_name}} 的 SMTP 测试邮件，用于验证当前配置是否可用。\n\n"
            "收件人：{{to_email}}\n"
            "发送时间：{{sent_at}}"
        ),
        "html_body": (
            "<div style='font-family:Arial,sans-serif;line-height:1.7;color:#0f172a'>"
            "{{site_logo}}"
            "<p>这是一封来自 <strong>{{site_name}}</strong> 的 SMTP 测试邮件，用于验证当前配置是否可用。</p>"
            "<p>收件人：{{to_email}}</p>"
            "<p>发送时间：{{sent_at}}</p>"
            "</div>"
        ),
        "i18n": {
            "en-US": {
                "label": "SMTP Test",
                "description": "Used to verify that the current SMTP configuration is available.",
                "subject": "SMTP test email",
                "text_body": (
                    "This is an SMTP test email from {{site_name}} used to verify the current configuration.\n\n"
                    "Recipient: {{to_email}}\n"
                    "Sent at: {{sent_at}}"
                ),
                "html_body": (
                    "<div style='font-family:Arial,sans-serif;line-height:1.7;color:#0f172a'>"
                    "{{site_logo}}"
                    "<p>This is an SMTP test email from <strong>{{site_name}}</strong> used to verify the current configuration.</p>"
                    "<p>Recipient: {{to_email}}</p>"
                    "<p>Sent at: {{sent_at}}</p>"
                    "</div>"
                ),
            }
        },
    },
]

_EMAIL_TEMPLATE_LOOKUP = {item["key"]: item for item in EMAIL_TEMPLATE_DEFINITIONS}
_EDITABLE_TEMPLATE_KEYS = {
    EmailTemplateKey.EMAIL_VERIFICATION.value,
    EmailTemplateKey.PASSWORD_CHANGED.value,
    EmailTemplateKey.INVITATION.value,
}
_TEMPLATE_PATTERN = re.compile(r"{{\s*([a-zA-Z0-9_]+)\s*}}")


def _definition_value(definition: dict[str, Any], field: str, language: str) -> Any:
    if language == "zh-CN":
        return definition.get(field)
    localized = (
        definition.get("i18n", {}).get(language, {}).get(field)
        if isinstance(definition.get("i18n"), dict)
        else None
    )
    return localized if localized is not None else definition.get(field)


def _definition_matches_builtin(definition: dict[str, Any], field: str, value: Any) -> bool:
    normalized = _normalize_template_value_for_matching(field, value)
    if not normalized:
        return False
    for candidate in _definition_match_candidates(definition, field):
        if candidate == normalized:
            return True
    return False


def _definition_match_candidates(definition: dict[str, Any], field: str) -> set[str]:
    candidates: set[str] = set()
    for language in SUPPORTED_SITE_LANGUAGES:
        current = _normalize_template_value_for_matching(
            field, _definition_value(definition, field, language)
        )
        if current:
            candidates.add(current)
        legacy = (
            definition.get("legacy_i18n", {}).get(language, {}).get(field)
            if isinstance(definition.get("legacy_i18n"), dict)
            else None
        )
        legacy_normalized = _normalize_template_value_for_matching(field, legacy)
        if legacy_normalized:
            candidates.add(legacy_normalized)
    return candidates


def _normalize_template_value_for_matching(field: str, value: Any) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    if field != "html_body":
        return normalized
    normalized = re.sub(r">\s+<", "><", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def list_email_template_configs(
    raw_items: Any, language: str | None = None
) -> list[dict[str, Any]]:
    resolved_language = normalize_site_language(language)
    normalized = normalize_email_templates(raw_items)
    normalized_map = {item["key"]: item for item in normalized}
    items: list[dict[str, Any]] = []

    for definition in _editable_email_template_definitions():
        value = normalized_map[definition["key"]]
        subject = value["subject"]
        html_body = value["html_body"]
        text_body = value.get("text_body")
        if _definition_matches_builtin(definition, "subject", subject):
            subject = _definition_value(definition, "subject", resolved_language)
        if _definition_matches_builtin(definition, "html_body", html_body):
            html_body = _definition_value(definition, "html_body", resolved_language)
        if _definition_matches_builtin(definition, "text_body", text_body):
            text_body = _definition_value(definition, "text_body", resolved_language)
        items.append(
            {
                "key": definition["key"],
                "label": _definition_value(definition, "label", resolved_language),
                "description": _definition_value(definition, "description", resolved_language),
                "variables": list(definition["variables"]),
                "subject": subject,
                "text_body": text_body,
                "html_body": html_body,
                "default_subject": _definition_value(definition, "subject", resolved_language),
                "default_text_body": _definition_value(definition, "text_body", resolved_language),
                "default_html_body": _definition_value(definition, "html_body", resolved_language),
            }
        )

    return items


def normalize_email_templates(raw_items: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_items, list):
        raw_items = []

    raw_map = {
        str(item.get("key")): item
        for item in raw_items
        if isinstance(item, dict) and item.get("key") in _EDITABLE_TEMPLATE_KEYS
    }

    items: list[dict[str, Any]] = []
    for definition in _editable_email_template_definitions():
        raw_item = raw_map.get(definition["key"], {})
        subject = str(raw_item.get("subject") or definition["subject"]).strip()
        html_body = str(raw_item.get("html_body") or definition["html_body"]).strip()
        text_body = raw_item.get("text_body")
        text_body = str(text_body).strip() if text_body is not None else None
        items.append(
            {
                "key": definition["key"],
                "subject": subject or definition["subject"],
                "html_body": html_body or definition["html_body"],
                "text_body": text_body,
            }
        )
    return items


def render_email_template(
    raw_items: Any,
    key: EmailTemplateKey | str,
    context: Mapping[str, Any] | None = None,
) -> dict[str, str | None]:
    template_key = key.value if isinstance(key, EmailTemplateKey) else str(key)
    definition = _EMAIL_TEMPLATE_LOOKUP.get(template_key)
    if not definition:
        raise ValueError(f"Unknown email template key: {template_key}")

    normalized_map = {item["key"]: item for item in normalize_email_templates(raw_items)}
    value = normalized_map.get(template_key)
    if not value:
        value = {
            "key": definition["key"],
            "subject": definition["subject"],
            "html_body": definition["html_body"],
            "text_body": definition.get("text_body"),
        }

    text_body = value.get("text_body")
    if text_body is None:
        text_body = definition.get("text_body")

    render_context = {
        key: _stringify_template_value(value) for key, value in (context or {}).items()
    }

    return {
        "subject": _render_template_text(value["subject"], render_context),
        "html_body": _render_template_text(value["html_body"], render_context),
        "text_body": _render_template_text(text_body, render_context),
    }


def build_email_template_context(settings_row: Any, **context: Any) -> dict[str, str]:
    site_name = str(getattr(settings_row, "site_name", None) or "Movary").strip() or "Movary"
    site_url = get_site_base_url(settings_row)
    site_logo_url = _resolve_template_asset_url(
        settings_row, getattr(settings_row, "site_logo_url", None)
    )

    merged_context = {
        "site_name": site_name,
        "site_url": site_url,
        "site_logo_url": site_logo_url,
        "site_logo": _build_site_logo_markup(site_logo_url, site_name),
    }
    merged_context.update({key: _stringify_template_value(value) for key, value in context.items()})
    return merged_context


def default_email_template_items() -> list[dict[str, Any]]:
    return deepcopy(
        [
            {
                "key": definition["key"],
                "subject": definition["subject"],
                "text_body": definition.get("text_body"),
                "html_body": definition["html_body"],
            }
            for definition in _editable_email_template_definitions()
        ]
    )


def _editable_email_template_definitions() -> list[dict[str, Any]]:
    return [
        definition
        for definition in EMAIL_TEMPLATE_DEFINITIONS
        if definition["key"] in _EDITABLE_TEMPLATE_KEYS
    ]


def _render_template_text(template: str | None, context: Mapping[str, str]) -> str | None:
    if template is None:
        return None
    return _TEMPLATE_PATTERN.sub(lambda match: context.get(match.group(1), ""), template)


def _stringify_template_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _resolve_template_asset_url(settings_row: Any, value: Any) -> str:
    normalized = _stringify_template_value(value).strip()
    if not normalized:
        return ""
    if normalized.startswith(("http://", "https://", "data:", "cid:", "//")):
        return normalized
    return build_site_url(settings_row, normalized)


def _build_site_logo_markup(site_logo_url: str, site_name: str) -> str:
    if not site_logo_url:
        return ""
    escaped_url = escape(site_logo_url, quote=True)
    escaped_name = escape(site_name or "Movary", quote=True)
    return (
        "<img "
        f"src='{escaped_url}' "
        f"alt='{escaped_name}' "
        "style='display:block;width:auto;max-width:56px;max-height:56px;"
        "margin:0 0 18px;border-radius:14px' />"
    )
