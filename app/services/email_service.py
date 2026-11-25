import smtplib
import ssl
from email.message import EmailMessage

from app.core.config import get_settings


def send_reset_email(recipient: str, link: str) -> None:
    send_email(recipient, "Reset Your StayOps Password",
        "A password reset was requested for your StayOps account.\n\n"
        f"Choose a new password using this link:\n{link}\n\n"
        f"This link expires in {get_settings().reset_token_expire_minutes} minutes and can be used once.\n"
        "If you did not request this, ignore this email. Your password has not changed.\n")


def send_tenant_link_email(recipient: str, link: str) -> None:
    send_email(recipient, "Link your StayOps tenant profile",
        f"Sign in with your existing tenant account, then open this link to connect your profile:\n{link}\n\n"
        "This link expires in 20 minutes and can be used once. Ignore it if you did not expect an invitation.")


def send_staff_setup_email(recipient: str, link: str) -> None:
    send_email(recipient, 'Set up your StayOps staff account',
        f'You have been invited to the StayOps staff team. Choose your password using this secure link:\n{link}\n\n'
        f'This link expires in {get_settings().reset_token_expire_minutes} minutes and can be used once. '
        'If your account is inactive, contact management to activate it and resend your invitation. '
        'If you did not expect this invitation, ignore this message.')


def send_staff_link_email(recipient: str, link: str) -> None:
    send_email(recipient, 'Confirm your StayOps staff profile',
        f'Management requested a staff profile link. Sign in with your existing account, then open:\n{link}\n\n'
        f'This single-use link expires in {get_settings().reset_token_expire_minutes} minutes. '
        'Your password and access have not been changed. Ignore this message if the request is unexpected.')


def send_email(recipient: str, subject: str, body: str) -> None:
    settings = get_settings()
    if not settings.smtp_host or not settings.smtp_from_email or settings.smtp_from_email.startswith("<"):
        raise RuntimeError("SMTP is not configured")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.smtp_from_email
    message["To"] = recipient
    message.set_content(body)
    # Port 465 supports implicit TLS; other ports use STARTTLS when enabled.
    context = ssl.create_default_context()
    factory = smtplib.SMTP_SSL if settings.smtp_use_tls and settings.smtp_port == 465 else smtplib.SMTP
    kwargs = {"context": context} if factory is smtplib.SMTP_SSL else {}
    with factory(settings.smtp_host, settings.smtp_port, timeout=10, **kwargs) as smtp:
        if settings.smtp_use_tls and factory is smtplib.SMTP:
            smtp.starttls(context=context)
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password.get_secret_value())
        smtp.send_message(message)
