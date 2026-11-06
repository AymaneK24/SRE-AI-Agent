"""Public pages' data, sign-in, sign-up, email verification, password reset, invitations, and SSO."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import secrets
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from ..auth import (
    client_ip, consume_email_token, create_email_token, create_organization, create_session, end_session, hash_password,
    normalize_email, org_policies, password_problem, verify_password,
)
from ..db import PLATFORM_ORG, sha256_text, utc_after, utcnow
from ..mailer import MailError
from ..net_guard import BlockedTarget, check_url, guarded_transport
from ..validation import Label, PersonName, name_from_email, sanitize_stored_name
from .deps import AppContext, Ctx, User

router = APIRouter(prefix="/api")
LOGIN_FAIL_LIMIT = 5
LOGIN_FAIL_WINDOW_S = 900
LOGIN_IP_LIMIT = 50
LOGIN_ALERT_AT = 20
LOGIN_MAX_DELAY_S = 8


class LoginBody(BaseModel):
    email: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class SignupBody(BaseModel):
    company: Label = Field(min_length=2, max_length=120)
    name: PersonName = Field(max_length=120)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)
    accept_terms: bool


class EmailBody(BaseModel):
    email: str = Field(min_length=3, max_length=254)


class TokenBody(BaseModel):
    token: str = Field(min_length=10, max_length=200)


class ResetBody(TokenBody):
    password: str = Field(min_length=1, max_length=256)


class AcceptBody(ResetBody):
    name: PersonName = Field(max_length=120)


class ChangePasswordBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class ProfileBody(BaseModel):
    name: PersonName | None = Field(default=None, max_length=120)
    email: str | None = Field(default=None, max_length=254)
    # Needed to change the email, which is how someone signs in and recovers the account.
    current_password: str | None = Field(default=None, max_length=256)


def _link(ctx: AppContext, route: str, token: str) -> str:
    return f"{ctx.config.public_url}/#/{route}?token={token}"


async def _send_verification(ctx: AppContext, user_id: int, email: str) -> None:
    token = await create_email_token(ctx.db, "verify", user_id, 24)
    await ctx.mailer.send(email, "Verify your Root Cause Console email",
                          "Confirm this email address to finish creating your account. The link works for 24 hours.\n\n"
                          "If you didn't sign up, you can ignore this message.", _link(ctx, "verify", token), "Verify email", security_notice=True)


async def _company_name(ctx: AppContext) -> str:
    """The name shown on public pages, only when the installation belongs to one company."""
    if not ctx.config.single_company:
        return ""
    row = await ctx.db.one("SELECT name FROM organizations ORDER BY id LIMIT 1")
    return row["name"] if row else ""


@router.get("/public/info")
async def public_info(ctx: Ctx) -> dict[str, Any]:
    signup = await ctx.db.get_setting(PLATFORM_ORG, "signup_open", ctx.config.signup_open)
    return {
        "product": "Root Cause Console", "creator": ctx.config.product_operator, "contact_email": ctx.config.contact_email,
        "signup_open": bool(signup) and not ctx.config.single_company,
        "single_company": ctx.config.single_company, "company": await _company_name(ctx), "email_configured": ctx.mailer.configured,
        "legal_reviewed": ctx.config.legal_reviewed,
    }


@router.post("/auth/login")
async def login(body: LoginBody, request: Request, response: Response, ctx: Ctx) -> dict[str, Any]:
    identifier = body.email.strip().lower()
    ip = client_ip(request, ctx.config)
    # A limit for this address and this account, and one for this address overall, stop the person guessing.
    # Failures from elsewhere only slow an account down. They never lock its owner out, so nobody can block someone by failing on purpose.
    key = "login:" + sha256_text(f"{identifier}|{ip}")
    ip_key = "login-ip:" + sha256_text(ip)
    account_key = "login-account:" + sha256_text(identifier)
    if ctx.limiter.count(key, LOGIN_FAIL_WINDOW_S) >= LOGIN_FAIL_LIMIT or ctx.limiter.count(ip_key, LOGIN_FAIL_WINDOW_S) >= LOGIN_IP_LIMIT:
        raise HTTPException(429, "Too many failed attempts from this device. Wait 15 minutes, or reset your password.")
    failures = ctx.limiter.count(account_key, LOGIN_FAIL_WINDOW_S)
    if failures >= LOGIN_FAIL_LIMIT:
        await asyncio.sleep(min(2 ** (failures - LOGIN_FAIL_LIMIT), LOGIN_MAX_DELAY_S))
    user = await ctx.db.one(
        "SELECT u.*, o.status AS org_status FROM users u JOIN organizations o ON o.id = u.org_id WHERE u.email = ? LIMIT 1", (identifier,))
    valid = verify_password(user["password_hash"] if user else "", body.password)
    if not user or not valid or not user["is_active"]:
        ctx.limiter.record(key)
        ctx.limiter.record(ip_key)
        ctx.limiter.record(account_key)
        if user:
            await ctx.db.audit(user["org_id"], "sign_in_failed", f"user:{user['id']}", {"ip": ip}, actor=user["email"])
            if failures + 1 >= LOGIN_ALERT_AT and ctx.limiter.allow(f"login-alert:{user['id']}", 1, 3600):
                with contextlib.suppress(MailError):
                    await ctx.mailer.send(user["email"], "Failed sign-ins on your Root Cause Console account",
                                          f"Hello {user['name']},\n\nWe've seen repeated failed attempts to sign in to your Root Cause Console account with the wrong password.\n\n"
                                          "Your account isn't locked, and you can still sign in as usual. If these attempts weren't you, change your password and tell your administrator.",
                                          f"{ctx.config.public_url}/#/signin", "Sign in", security_notice=True)
        raise HTTPException(401, "That email or password isn't correct.")
    if not user["email_verified_at"]:
        raise HTTPException(403, "Verify your email first. Check your inbox for the link, or request a new one.")
    if user["org_status"] != "active" and not user["is_platform_admin"]:
        raise HTTPException(403, "This organization has been suspended. Contact the service operator.")
    policies = await org_policies(ctx.db, user["org_id"])
    if policies.get("require_sso") and not user["is_platform_admin"]:
        raise HTTPException(403, "Your organization requires single sign-on. Use Sign in with SSO.")
    ctx.limiter.clear(key)
    ctx.limiter.clear(account_key)
    await create_session(ctx.db, ctx.config, response, request, user)
    await ctx.db.audit(user["org_id"], "sign_in", f"user:{user['id']}", {"ip": ip}, actor=user["email"])
    return {"authenticated": True, "must_change_password": bool(user["must_change_password"])}


@router.post("/auth/logout")
async def logout(request: Request, response: Response, ctx: Ctx, user: User) -> dict[str, bool]:
    await end_session(ctx.db, request, response)
    await ctx.db.audit(user.org_id, "sign_out", f"user:{user.user_id}", {}, actor=user.actor)
    return {"authenticated": False}


@router.get("/auth/me")
async def me(user: User, ctx: Ctx) -> dict[str, Any]:
    row = await ctx.db.one("SELECT email_verified_at, pending_email, sso_subject FROM users WHERE id = ?", (user.user_id,))
    return {
        "id": user.user_id, "org_id": user.org_id, "email": user.email, "name": user.name, "role": user.role, "org_name": user.org_name,
        "must_change_password": user.must_change_password, "email_verified": bool(row["email_verified_at"]),
        "pending_email": row["pending_email"], "uses_sso": bool(row["sso_subject"]),
    }


@router.patch("/auth/me")
async def update_me(body: ProfileBody, request: Request, response: Response, user: User, ctx: Ctx) -> dict[str, Any]:
    outcome: dict[str, Any] = {"updated": True, "pending_email": None}
    if body.email:
        outcome = await _change_email(body, request, response, user, ctx)
    if body.name:
        await ctx.db.execute("UPDATE users SET name = ? WHERE id = ?", (body.name, user.user_id))
    await ctx.db.audit(user.org_id, "profile_updated", f"user:{user.user_id}", {"fields": [key for key in ("name", "email") if getattr(body, key)]}, actor=user.actor)
    return outcome


async def _change_email(body: ProfileBody, request: Request, response: Response, user: Any, ctx: AppContext) -> dict[str, Any]:
    """The email signs people in and resets passwords, so a stolen session alone must not be able to change it."""
    email = normalize_email(body.email or "")
    row = await ctx.db.one("SELECT * FROM users WHERE id = ?", (user.user_id,))
    if row["sso_subject"]:
        raise HTTPException(409, "Your email comes from single sign-on, so change it there.")
    if not ctx.limiter.allow(f"email-change:{user.user_id}", LOGIN_FAIL_LIMIT, LOGIN_FAIL_WINDOW_S):
        raise HTTPException(429, "Too many attempts. Wait 15 minutes and try again.")
    if not body.current_password or not verify_password(row["password_hash"], body.current_password):
        raise HTTPException(401, "Your current password isn't correct.")
    if email == row["email"]:
        return {"updated": True, "pending_email": None}
    if await ctx.db.one("SELECT id FROM users WHERE email = ? AND id <> ?", (email, user.user_id)):
        raise HTTPException(409, "Another account already uses this email address.")
    domains = [item.lower() for item in (await org_policies(ctx.db, user.org_id)).get("allowed_email_domains", [])]
    if domains and email.split("@")[-1] not in domains:
        raise HTTPException(422, f"Your organization's policy only allows these email domains: {', '.join(domains)}.")
    if not ctx.mailer.configured:
        # No email server means no reset links can leave this server, so there's nothing to confirm by email.
        await ctx.db.execute("UPDATE users SET email = ?, username = ?, pending_email = NULL WHERE id = ?", (email, email, user.user_id))
        await ctx.db.execute("DELETE FROM sessions WHERE user_id = ?", (user.user_id,))
        await create_session(ctx.db, ctx.config, response, request, await ctx.db.one("SELECT * FROM users WHERE id = ?", (user.user_id,)))
        await ctx.db.audit(user.org_id, "email_changed", f"user:{user.user_id}", {"to": email}, actor=email)
        return {"updated": True, "pending_email": None}
    # The sign-in address stays the same until the new address proves it can receive mail.
    await ctx.db.execute("UPDATE users SET pending_email = ? WHERE id = ?", (email, user.user_id))
    token = await create_email_token(ctx.db, "email_change", user.user_id, 24)
    try:
        await ctx.mailer.send(email, "Confirm your new Root Cause Console email",
                              f"Hello {row['name']},\n\nYou asked to use this address to sign in to Root Cause Console.\n\n"
                              "To confirm it, use the button below. This link is personal to you and expires in 24 hours. You'll then need to sign in again on all your devices.\n\n"
                              "If you didn't ask for this, you can ignore this message. Your email stays the same.",
                              _link(ctx, "verify", token), "Confirm email", security_notice=True)
    except MailError as error:
        await ctx.db.execute("UPDATE users SET pending_email = NULL WHERE id = ?", (user.user_id,))
        raise HTTPException(502, "The email server didn't accept the message, so your email wasn't changed.") from error
    with contextlib.suppress(MailError):
        await ctx.mailer.send(row["email"], "Your Root Cause Console email is being changed",
                              f"Hello {row['name']},\n\nSomeone signed in to your Root Cause Console account and asked to change its email to {email}.\n\n"
                              "Nothing changes until that address is confirmed. If this wasn't you, change your password now and tell your administrator.", f"{ctx.config.public_url}/#/signin", "Sign in", security_notice=True)
    await ctx.db.audit(user.org_id, "email_change_requested", f"user:{user.user_id}", {"to": email}, actor=user.actor)
    return {"updated": True, "pending_email": email}


@router.post("/auth/change-password")
async def change_password(body: ChangePasswordBody, request: Request, response: Response, user: User, ctx: Ctx) -> dict[str, bool]:
    row = await ctx.db.one("SELECT * FROM users WHERE id = ?", (user.user_id,))
    if not verify_password(row["password_hash"], body.current_password):
        raise HTTPException(401, "Your current password isn't correct.")
    if body.current_password == body.new_password:
        raise HTTPException(422, "Choose a password different from your current one.")
    policies = await org_policies(ctx.db, user.org_id)
    problem = password_problem(body.new_password, policies, row["email"], row["name"])
    if problem:
        raise HTTPException(422, problem)
    await ctx.db.execute("UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?", (hash_password(body.new_password), user.user_id))
    await ctx.db.execute("DELETE FROM sessions WHERE user_id = ?", (user.user_id,))
    fresh = await ctx.db.one("SELECT * FROM users WHERE id = ?", (user.user_id,))
    await create_session(ctx.db, ctx.config, response, request, fresh)
    await ctx.db.audit(user.org_id, "password_changed", f"user:{user.user_id}", {}, actor=user.actor)
    return {"changed": True}


@router.post("/auth/signup", status_code=202)
async def signup(body: SignupBody, request: Request, ctx: Ctx) -> dict[str, Any]:
    if ctx.config.single_company or not await ctx.db.get_setting(PLATFORM_ORG, "signup_open", ctx.config.signup_open):
        raise HTTPException(403, "New sign-ups are closed. Ask your administrator for an invitation.")
    if not ctx.limiter.allow("signup:" + client_ip(request, ctx.config), 5, 3600):
        raise HTTPException(429, "Too many sign-ups from this address. Try again in an hour.")
    if not body.accept_terms:
        raise HTTPException(422, "Accept the terms and privacy policy to continue.")
    email = normalize_email(body.email)
    problem = password_problem(body.password, {"password_min_length": 12}, email, body.name)
    if problem:
        raise HTTPException(422, problem)
    generic = {"accepted": True, "email_sent": ctx.mailer.configured}
    if await ctx.db.one("SELECT id FROM users WHERE email = ?", (email,)):
        # The response is the same, so nobody can use this form to discover who has an account.
        await ctx.mailer.send(email, "Someone tried to sign up with your email",
                              "This address already has a Root Cause Console account. If that was you, sign in or reset your password.",
                              f"{ctx.config.public_url}/#/signin", "Sign in", security_notice=True)
        return generic
    org_id = await create_organization(ctx.db, body.company.strip(), email)
    user_id = await ctx.db.insert(
        "INSERT INTO users(org_id, username, email, name, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?, 'owner', ?)",
        (org_id, email, email, body.name, hash_password(body.password), utcnow()),
    )
    await ctx.db.audit(org_id, "organization_created", f"org:{org_id}", {"name": body.company.strip()}, actor=email)
    await _send_verification(ctx, user_id, email)
    return generic


@router.post("/auth/verify")
async def verify(body: TokenBody, ctx: Ctx) -> dict[str, bool]:
    changing = await consume_email_token(ctx.db, "email_change", body.token)
    if changing:
        email = changing["pending_email"]
        if not email or await ctx.db.one("SELECT id FROM users WHERE email = ? AND id <> ?", (email, changing["id"])):
            raise HTTPException(400, "This link is invalid or has expired. Request a new one.")
        await ctx.db.execute("UPDATE users SET email = ?, username = ?, pending_email = NULL WHERE id = ?", (email, email, changing["id"]))
        await ctx.db.execute("DELETE FROM sessions WHERE user_id = ?", (changing["id"],))
        await ctx.db.audit(changing["org_id"], "email_changed", f"user:{changing['id']}", {"from": changing["email"], "to": email}, actor=email)
        return {"verified": True, "email_changed": True}
    user = await consume_email_token(ctx.db, "verify", body.token)
    if not user:
        raise HTTPException(400, "This link is invalid or has expired. Request a new one.")
    await ctx.db.execute("UPDATE users SET email_verified_at = ? WHERE id = ?", (utcnow(), user["id"]))
    await ctx.db.audit(user["org_id"], "email_verified", f"user:{user['id']}", {}, actor=user["email"])
    return {"verified": True}


@router.post("/auth/resend-verification", status_code=202)
async def resend(body: EmailBody, request: Request, ctx: Ctx) -> dict[str, bool]:
    if not ctx.limiter.allow("resend:" + client_ip(request, ctx.config), 5, 3600):
        raise HTTPException(429, "Too many requests. Try again in an hour.")
    email = normalize_email(body.email)
    row = await ctx.db.one("SELECT id FROM users WHERE email = ? AND email_verified_at IS NULL", (email,))
    if row:
        await _send_verification(ctx, row["id"], email)
    return {"accepted": True}


@router.post("/auth/forgot-password", status_code=202)
async def forgot(body: EmailBody, request: Request, ctx: Ctx) -> dict[str, bool]:
    if not ctx.limiter.allow("forgot:" + client_ip(request, ctx.config), 5, 3600):
        raise HTTPException(429, "Too many requests. Try again in an hour.")
    email = normalize_email(body.email)
    row = await ctx.db.one("SELECT id, name FROM users WHERE email = ? AND is_active = 1", (email,))
    if row:
        token = await create_email_token(ctx.db, "reset", row["id"], 1)
        await ctx.mailer.send(email, "Reset your Root Cause Console password",
                              f"Hello {row['name']},\n\nWe received a request to reset the password for your Root Cause Console account.\n\n"
                              "To choose a new password, use the button below. This link is personal to you and expires in one hour.\n\n"
                              "If you didn't ask for this, you can ignore this message. Your password stays the same.",
                              _link(ctx, "reset", token), "Reset password", security_notice=True)
    return {"accepted": True}


@router.post("/auth/reset-password")
async def reset(body: ResetBody, ctx: Ctx) -> dict[str, bool]:
    token_row = await ctx.db.one(
        "SELECT u.* FROM email_tokens t JOIN users u ON u.id = t.user_id WHERE t.token_hash = ? AND t.purpose = 'reset' AND t.used_at IS NULL", (sha256_text(body.token),))
    if token_row:
        policies = await org_policies(ctx.db, token_row["org_id"])
        problem = password_problem(body.password, policies, token_row["email"], token_row["name"])
        if problem:
            raise HTTPException(422, problem)
    user = await consume_email_token(ctx.db, "reset", body.token)
    if not user:
        raise HTTPException(400, "This link is invalid or has expired. Request a new one.")
    await ctx.db.execute("UPDATE users SET password_hash = ?, must_change_password = 0, email_verified_at = COALESCE(email_verified_at, ?) WHERE id = ?", (hash_password(body.password), utcnow(), user["id"]))
    await ctx.db.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
    await ctx.db.audit(user["org_id"], "password_reset", f"user:{user['id']}", {}, actor=user["email"])
    return {"reset": True}


@router.post("/auth/accept-invite")
async def accept_invite(body: AcceptBody, request: Request, response: Response, ctx: Ctx) -> dict[str, bool]:
    pending = await ctx.db.one(
        "SELECT u.* FROM email_tokens t JOIN users u ON u.id = t.user_id WHERE t.token_hash = ? AND t.purpose = 'invite' AND t.used_at IS NULL", (sha256_text(body.token),))
    if pending:
        policies = await org_policies(ctx.db, pending["org_id"])
        problem = password_problem(body.password, policies, pending["email"], body.name)
        if problem:
            raise HTTPException(422, problem)
    user = await consume_email_token(ctx.db, "invite", body.token)
    if not user:
        raise HTTPException(400, "This invitation is invalid or has expired. Ask your admin for a new one.")
    await ctx.db.execute("UPDATE users SET name = ?, password_hash = ?, is_active = 1, email_verified_at = ? WHERE id = ?",
                         (body.name, hash_password(body.password), utcnow(), user["id"]))
    fresh = await ctx.db.one("SELECT * FROM users WHERE id = ?", (user["id"],))
    await create_session(ctx.db, ctx.config, response, request, fresh)
    await ctx.db.audit(user["org_id"], "invitation_accepted", f"user:{user['id']}", {}, actor=user["email"])
    return {"authenticated": True}


# Single sign-on with OpenID Connect ------------------------------------------
async def _sso_settings(ctx: AppContext, domain: str) -> tuple[int, dict[str, Any]] | None:
    rows = await ctx.db.all("SELECT org_id, value FROM settings WHERE key = 'sso'")
    for row in rows:
        settings = json.loads(row["value"])
        if settings.get("enabled") and domain in [item.lower() for item in settings.get("domains", [])]:
            return row["org_id"], settings
    return None


async def _discover(issuer: str, ctx: AppContext) -> dict[str, Any]:
    await check_url(issuer, ctx.config.target_policy)
    async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False, transport=guarded_transport(ctx.config.target_policy)) as client:
        response = await client.get(issuer.rstrip("/") + "/.well-known/openid-configuration")
    if response.status_code != 200:
        raise HTTPException(502, "The identity provider didn't return its settings. Check the issuer address.")
    return response.json()


@router.post("/auth/sso/start")
async def sso_start(body: EmailBody, ctx: Ctx) -> dict[str, str]:
    email = normalize_email(body.email)
    found = await _sso_settings(ctx, email.split("@")[-1])
    if not found:
        raise HTTPException(404, "Single sign-on isn't set up for this email domain.")
    org_id, settings = found
    try:
        metadata = await _discover(settings["issuer"], ctx)
    except (httpx.HTTPError, BlockedTarget) as error:
        raise HTTPException(502, f"Could not reach the identity provider: {error}") from error
    state, nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(16), secrets.token_urlsafe(48)
    await ctx.db.execute("INSERT INTO sso_states(state_hash, org_id, nonce, verifier, expires_at) VALUES (?, ?, ?, ?, ?)",
                         (sha256_text(state), org_id, nonce, verifier, utc_after(minutes=10)))
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    params = httpx.QueryParams({
        "response_type": "code", "client_id": settings["client_id"], "redirect_uri": f"{ctx.config.public_url}/api/auth/sso/callback",
        "scope": "openid email profile", "state": state, "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256", "login_hint": email,
    })
    return {"redirect": f"{metadata['authorization_endpoint']}?{params}"}


@router.get("/auth/sso/callback")
async def sso_callback(request: Request, ctx: Ctx, state: str = "", code: str = "", error: str = "") -> Response:
    failure = RedirectResponse(f"{ctx.config.public_url}/#/signin?sso=failed", status_code=302)
    if error or not state or not code:
        return failure
    row = await ctx.db.one("SELECT * FROM sso_states WHERE state_hash = ?", (sha256_text(state),))
    if not row:
        return failure
    await ctx.db.execute("DELETE FROM sso_states WHERE state_hash = ?", (sha256_text(state),))
    if datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00")) < datetime.now(timezone.utc):
        return failure
    org_id = row["org_id"]
    settings = await ctx.db.get_setting(org_id, "sso", {})
    if not settings or not settings.get("enabled"):
        return failure
    try:
        metadata = await _discover(settings["issuer"], ctx)
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False, transport=guarded_transport(ctx.config.target_policy)) as client:
            token_response = await client.post(metadata["token_endpoint"], data={
                "grant_type": "authorization_code", "code": code, "redirect_uri": f"{ctx.config.public_url}/api/auth/sso/callback",
                "client_id": settings["client_id"], "client_secret": ctx.box.decrypt(settings.get("client_secret_enc", "")), "code_verifier": row["verifier"],
            })
            if token_response.status_code != 200:
                return failure
            access = token_response.json().get("access_token")
            info_response = await client.get(metadata["userinfo_endpoint"], headers={"Authorization": f"Bearer {access}"})
        if info_response.status_code != 200:
            return failure
        info = info_response.json()
    except (httpx.HTTPError, ValueError, KeyError, BlockedTarget):
        return failure
    email = str(info.get("email", "")).lower()
    subject = str(info.get("sub", ""))
    if not email or info.get("email_verified") is False or not subject or email.split("@")[-1] not in [d.lower() for d in settings.get("domains", [])]:
        return failure
    user = await ctx.db.one("SELECT * FROM users WHERE email = ?", (email,))
    if user and user["org_id"] != org_id:
        return failure
    if not user:
        user_id = await ctx.db.insert(
            "INSERT INTO users(org_id, username, email, name, role, email_verified_at, sso_subject, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (org_id, email, email, sanitize_stored_name(str(info.get("name") or ""), name_from_email(email)), settings.get("default_role", "viewer"), utcnow(), subject, utcnow()),
        )
        await ctx.db.audit(org_id, "sso_user_created", f"user:{user_id}", {}, actor=email)
        user = await ctx.db.one("SELECT * FROM users WHERE id = ?", (user_id,))
    elif not user["is_active"]:
        return failure
    elif user["sso_subject"] and user["sso_subject"] != subject:
        return failure
    else:
        await ctx.db.execute("UPDATE users SET sso_subject = ?, email_verified_at = COALESCE(email_verified_at, ?) WHERE id = ?", (subject, utcnow(), user["id"]))
    success = RedirectResponse(f"{ctx.config.public_url}/#/overview", status_code=302)
    await create_session(ctx.db, ctx.config, success, request, user)
    await ctx.db.audit(org_id, "sign_in", f"user:{user['id']}", {"method": "sso"}, actor=email)
    return success
