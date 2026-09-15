"""Judge invitation redemption and renewal without changing the primary user identity."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass

from .models import OPAQUE, Config, Denied, Invitation, Session, Unavailable, digest
from .store import SessionStore
from .tokens import AccessToken, TokenIssuer


@dataclass(frozen=True, repr=False)
class IssuedSession:
    token: AccessToken
    cookie_value: str
    session: Session


class JudgeSessionService:
    def __init__(
        self,
        config: Config,
        *,
        store: SessionStore,
        issuer: TokenIssuer,
        clock: Callable[[], int],
        random_token: Callable[[], str] | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.issuer = issuer
        self.clock = clock
        self.random_token = random_token or (lambda: secrets.token_urlsafe(32))

    def _now(self) -> int:
        now = self.clock()
        if now >= self.config.campaign_expires_at:
            raise Denied
        return now

    def redeem(self, invitation_value: str) -> IssuedSession:
        if not isinstance(invitation_value, str) or OPAQUE.fullmatch(invitation_value) is None:
            raise Denied
        invitation_digest = digest(invitation_value)
        invitation = Invitation.read(
            self.store.invitation(invitation_digest), self.config, invitation_digest, self._now()
        )
        token = self.issuer.issue()
        now = self._now()
        expiry = min(
            now + self.config.duration_seconds,
            invitation.expires_at,
            self.config.campaign_expires_at,
        )
        if expiry <= now:
            raise Denied
        cookie = self.random_token()
        if not isinstance(cookie, str) or len(cookie) != 43 or OPAQUE.fullmatch(cookie) is None:
            raise Unavailable
        session = Session(digest(cookie), invitation_digest, now, expiry)
        # Never release the access token unless count reservation and session creation both win.
        self.store.reserve(invitation, session, now)
        return IssuedSession(token, cookie, session)

    def _session(self, cookie: str) -> Session:
        if not isinstance(cookie, str) or len(cookie) != 43 or OPAQUE.fullmatch(cookie) is None:
            raise Denied
        now = self._now()
        session = Session.read(self.store.session(digest(cookie)), self.config, digest(cookie), now)
        invitation = Invitation.read(
            self.store.invitation(session.invitation_digest),
            self.config,
            session.invitation_digest,
            now,
            redeem=False,
        )
        if session.expires_at > invitation.expires_at:
            raise Denied
        return session

    def refresh(self, cookie: str) -> IssuedSession:
        before = self._session(cookie)
        token = self.issuer.issue()
        # Sign-out or campaign revocation during the network request must win over refresh.
        after = self._session(cookie)
        if after != before:
            raise Denied
        return IssuedSession(token, cookie, after)

    def logout(self, cookie: str | None) -> None:
        # Local broker logout never revokes the shared primary OAuth refresh token.
        if cookie is not None and len(cookie) == 43 and OPAQUE.fullmatch(cookie):
            self.store.revoke(digest(cookie))
