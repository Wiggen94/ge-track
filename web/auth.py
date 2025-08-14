from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from passlib.hash import bcrypt

from .db import get_session
from .models import User

SECRET = os.environ.get("GE_TRACK_SECRET", "dev-secret-change-me")
COOKIE_NAME = "ge_track_session"
serializer = URLSafeTimedSerializer(SECRET)

router = APIRouter()


def get_user_from_cookie(request: Request) -> Optional[User]:
    cookie = request.cookies.get(COOKIE_NAME)
    if not cookie:
        return None
    try:
        data = serializer.loads(cookie, max_age=60 * 60 * 24 * 14)
    except (BadSignature, SignatureExpired):
        return None
    user_id = data.get("uid")
    from .db import get_session
    with get_session() as s:
        user = s.get(User, user_id)
        return user


@router.get("/signup", response_class=HTMLResponse)
async def signup_form(request: Request):
    # minimal inline form to keep focus on core app
    return HTMLResponse("""
        <h1>Sign up</h1>
        <form method='post'>
          <label>Email <input name='email'/></label>
          <label>Password <input name='password' type='password'/></label>
          <button type='submit'>Create</button>
        </form>
        <p><a href='/login'>Login</a></p>
    """)


@router.post("/signup")
async def signup(email: str = Form(...), password: str = Form(...)):
    email = email.strip().lower()
    if not email or not password:
        raise HTTPException(400, "Email and password required")
    with get_session() as s:
        existing = s.query(User).where(User.email == email).first()
        if existing:
            raise HTTPException(400, "Email already in use")
        user = User(email=email, password_hash=bcrypt.hash(password))
        s.add(user)
        s.commit()
        s.refresh(user)
        resp = RedirectResponse(url="/", status_code=302)
        resp.set_cookie(COOKIE_NAME, serializer.dumps({"uid": user.id}), httponly=True, max_age=60 * 60 * 24 * 14)
        return resp


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return HTMLResponse("""
        <h1>Login</h1>
        <form method='post'>
          <label>Email <input name='email'/></label>
          <label>Password <input name='password' type='password'/></label>
          <button type='submit'>Login</button>
        </form>
        <p><a href='/signup'>Sign up</a></p>
    """)


@router.post("/login")
async def login(email: str = Form(...), password: str = Form(...)):
    email = email.strip().lower()
    with get_session() as s:
        user = s.query(User).where(User.email == email).first()
        if not user or not bcrypt.verify(password, user.password_hash):
            raise HTTPException(401, "Invalid credentials")
        resp = RedirectResponse(url="/", status_code=302)
        resp.set_cookie(COOKIE_NAME, serializer.dumps({"uid": user.id}), httponly=True, max_age=60 * 60 * 24 * 14)
        return resp


@router.post("/logout")
async def logout():
    resp = RedirectResponse(url="/", status_code=302)
    resp.delete_cookie(COOKIE_NAME)
    return resp
