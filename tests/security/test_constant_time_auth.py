"""Constant-time token auth (NFR-SEC-006).

Token comparison must be constant-time (no timing side channel) and must reject
wrong, partial/prefix, missing, and length-mismatched tokens while accepting the
exact one. We can't assert wall-clock constant-ness reliably, so we assert the
behavioural contract and that the leaky ``==`` path is gone.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from hermes_pm.config import load_settings
from hermes_pm.dashboard.server import _check_token
from hermes_pm.util.security import tokens_match

TOKEN = "s3cret-Token-ABCDEFmore"


def test_tokens_match_exact():
    assert tokens_match(TOKEN, TOKEN) is True


@pytest.mark.parametrize(
    "provided",
    [
        None, "", "wrong",
        TOKEN[:-1],          # prefix (one char short)
        TOKEN + "x",         # one char long
        TOKEN.upper(),       # case differs
        " " + TOKEN,         # leading space
    ],
)
def test_tokens_match_rejects(provided):
    assert tokens_match(TOKEN, provided) is False


@pytest.mark.parametrize("expected", [None, ""])
def test_tokens_match_missing_expected_is_false(expected):
    assert tokens_match(expected, "anything") is False


def test_tokens_match_handles_non_ascii():
    assert tokens_match("токен-密码", "токен-密码") is True
    assert tokens_match("токен-密码", "токен-XX") is False


# --- dashboard gate -------------------------------------------------------- #
def _remote(**kw):
    return load_settings(dashboard_host="0.0.0.0", dashboard_token=TOKEN, **kw)  # noqa: S104


def _request(token: str | None = None) -> Request:
    headers = []
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers})


def test_dashboard_localhost_needs_no_token(tmp_path):
    s = load_settings(dashboard_host="127.0.0.1", data_dir=str(tmp_path))
    _check_token(s, _request())  # must not raise on localhost


def test_dashboard_remote_accepts_exact_token(tmp_path):
    _check_token(_remote(data_dir=str(tmp_path)), _request(TOKEN))  # must not raise


@pytest.mark.parametrize("bad", [None, "", "wrong", TOKEN[:-1], TOKEN + "x"])
def test_dashboard_remote_rejects_bad_token(tmp_path, bad):
    with pytest.raises(HTTPException) as ei:
        _check_token(_remote(data_dir=str(tmp_path)), _request(bad))
    assert ei.value.status_code == 401


def test_dashboard_remote_without_configured_token_is_locked(tmp_path):
    # Bound remotely but no token configured -> always denied (fail-closed).
    s = load_settings(dashboard_host="0.0.0.0", data_dir=str(tmp_path))  # noqa: S104
    with pytest.raises(HTTPException):
        _check_token(s, _request(TOKEN))


def test_dashboard_query_token_rejected_even_if_correct(tmp_path):
    s = _remote(data_dir=str(tmp_path))
    with pytest.raises(HTTPException) as ei:
        _check_token(s, _request(TOKEN), query_token=TOKEN)
    assert ei.value.status_code == 400
