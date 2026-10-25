"""Policy (spec §8): principal scopes, the single side-effect path ``Policy.execute``, and the
vendor bank-account invariants of §5.3. Every refusal is logged as an ``agent_action`` event
(``post_rejected`` or ``policy_violation``) before the exception propagates."""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Callable
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, NoReturn

import structlog

from core.events import EventStore, make_event
from core.rules import BUTTON_THRESHOLD_MINOR
from core.vendors import VendorBankAccount, VendorMaster, account_hash

if TYPE_CHECKING:
    from core.ledger import Evidence, JournalEntry, JournalEntryDraft, Ledger

log = structlog.get_logger("policy")


@dataclass(frozen=True)
class Scope:
    post: AbstractSet[str] = frozenset()
    request_payment: bool = False
    edit_vendor: bool = False
    edit_allowlist: bool = False
    approve: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "post", frozenset(self.post))

    def can_post(self, account: str) -> bool:
        return any(fnmatch.fnmatchcase(account, pattern) for pattern in self.post)


PRINCIPALS: dict[str, Scope] = {
    "invoice_agent": Scope(
        post={"Liabilities:AP*", "Liabilities:GRNI", "Liabilities:VAT:Output", "Expenses:*"},
        request_payment=False,
        edit_vendor=False,
        edit_allowlist=False,
    ),
    "vendor_agent": Scope(
        post=set(), request_payment=False, edit_vendor=True, edit_allowlist=False
    ),
    "payment_agent": Scope(
        post={
            "Assets:Bank:*",
            "Liabilities:AP*",
            "Liabilities:Intercompany",
            "Expenses:Bank:*",
            "Expenses:FX:*",
            "Income:Discounts",
        },
        request_payment=True,
        edit_vendor=False,
        edit_allowlist=False,
    ),
    "controller": Scope(post={"Equity:Opening"}, approve=True),
    "provisioner": Scope(edit_allowlist=True),
    "bootstrap": Scope(post={"*"}),  # opening balances only; refused once the clock has started
}


def account_in_scope(principal: str, account: str) -> bool:
    scope = PRINCIPALS.get(principal)
    return scope is not None and scope.can_post(account)


def jumper_present() -> bool:
    """Provisioning jumper. Until the hardware client lands (Part 7) this is the config flag
    ``PAYGUARD_JUMPER=1``; the demo pulls the jumper by unsetting it."""
    return os.environ.get("PAYGUARD_JUMPER", "0") == "1"


class PolicyViolation(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(
            f"PolicyViolation({reason}): {detail}" if detail else f"PolicyViolation({reason})"
        )
        self.reason = reason
        self.detail = detail


# ------------------------------------------------------------------------------------------
# Actions
# ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PostAction:
    draft: JournalEntryDraft
    posted_at: datetime | None = None


@dataclass(frozen=True)
class ReverseAction:
    entry_id: str
    cause_type: str
    cause_id: str
    effective_date: date
    posted_at: datetime | None = None
    extra_evidence: list[Evidence] | None = None


@dataclass(frozen=True)
class ClosePeriodAction:
    month: str


@dataclass(frozen=True)
class OpenPendingAccountAction:
    vendor_id: str
    iban_or_acct: str
    requested_via: str
    request_event_id: str | None
    hold_hours: int = 72


@dataclass(frozen=True)
class MarkVerifiedAction:
    account_id: str
    verified_via: str
    verified_event_id: str | None


@dataclass(frozen=True)
class ApproveAccountAction:
    account_id: str
    approval_event_id: str | None


@dataclass(frozen=True)
class ActivateIfReadyAction:
    account_id: str


@dataclass(frozen=True)
class SuspendAccountAction:
    account_id: str
    reason: str


@dataclass(frozen=True)
class RestorePriorAction:
    vendor_id: str


@dataclass(frozen=True)
class SetOnChipAction:
    account_id: str


@dataclass(frozen=True)
class RequestPaymentAction:
    """Ask the signer to authorise a payment to ``account_id`` (spec §7.5, §8)."""

    pay_id: str
    vendor_id: str
    account_id: str
    amount_minor: int
    currency: str
    eur_equiv_minor: int
    payable_ids: tuple[str, ...]


Action = (
    PostAction
    | ReverseAction
    | ClosePeriodAction
    | OpenPendingAccountAction
    | MarkVerifiedAction
    | ApproveAccountAction
    | ActivateIfReadyAction
    | SuspendAccountAction
    | RestorePriorAction
    | SetOnChipAction
    | RequestPaymentAction
)


class Policy:
    """The only path for side effects. Scope and invariants are checked here; refusals are events."""

    def __init__(
        self,
        *,
        ledger: Ledger,
        vendors: VendorMaster,
        events: EventStore,
        jumper_present: Callable[[], bool] = jumper_present,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._ledger = ledger
        self._vendors = vendors
        self._events = events
        self._jumper = jumper_present
        self._now: Callable[[], datetime] = now or (lambda: datetime.now(UTC))

    @property
    def ledger(self) -> Ledger:
        return self._ledger

    # ---------------------------------------------------------------- dispatch

    def execute(self, action: Action, principal: str) -> Any:
        scope = PRINCIPALS.get(principal)
        if principal == "bootstrap" and self._events.has_type("day_boundary"):
            self._violation(
                principal,
                action,
                "bootstrap_after_start",
                "the bootstrap principal may only act before the clock starts (a day_boundary exists)",
                refs=[],
            )
        if scope is None:
            self._violation(
                principal,
                action,
                "unknown_principal",
                f"{principal!r} is not a known principal",
                refs=[],
            )
        try:
            if isinstance(action, PostAction):
                return self._post(action, principal)
            if isinstance(action, ReverseAction):
                return self._reverse(action, principal)
            if isinstance(action, ClosePeriodAction):
                self._require(
                    scope.approve,
                    principal,
                    action,
                    "approve_required",
                    "closing a period needs approve scope",
                    [action.month],
                )
                self._ledger.close_period(action.month, principal)
                return None
            if isinstance(action, OpenPendingAccountAction):
                return self._open_pending(action, principal, scope)
            if isinstance(action, MarkVerifiedAction):
                return self._mark_verified(action, principal, scope)
            if isinstance(action, ApproveAccountAction):
                return self._approve(action, principal, scope)
            if isinstance(action, ActivateIfReadyAction):
                return self._activate(action, principal, scope)
            if isinstance(action, SuspendAccountAction):
                return self._suspend(action, principal, scope)
            if isinstance(action, RestorePriorAction):
                return self._restore_prior(action, principal, scope)
            if isinstance(action, SetOnChipAction):
                return self._set_on_chip(action, principal, scope)
            if isinstance(action, RequestPaymentAction):
                return self._request_payment(action, principal, scope)
        except PolicyViolation:
            raise

    # ---------------------------------------------------------------- posting

    def _post(self, action: PostAction, principal: str) -> JournalEntry:
        from core.ledger import PostRejected

        try:
            return self._ledger.post(action.draft, principal, posted_at=action.posted_at)
        except PostRejected as exc:
            if exc.reason_code == "duplicate":
                log.info(
                    "post_duplicate",
                    principal=principal,
                    existing_entry_id=exc.existing_entry_id,
                    event_id=None,
                    rule_id=None,
                )
                raise
            self._log_post_rejected(
                principal,
                exc.reason_code,
                exc.detail,
                refs=[action.draft.cause_id, *sorted({ln.account for ln in action.draft.lines})],
                inputs={
                    "effective_date": action.draft.effective_date.isoformat(),
                    "cause_type": action.draft.cause_type,
                    "lines": [
                        [ln.account, ln.debit_minor, ln.credit_minor, ln.currency]
                        for ln in action.draft.lines
                    ],
                },
            )
            raise

    def _reverse(self, action: ReverseAction, principal: str) -> JournalEntry:
        from core.ledger import PostRejected

        try:
            return self._ledger.reverse(
                action.entry_id,
                principal,
                action.cause_type,
                action.cause_id,
                action.effective_date,
                posted_at=action.posted_at,
                extra_evidence=action.extra_evidence,
            )
        except PostRejected as exc:
            self._log_post_rejected(
                principal,
                exc.reason_code,
                exc.detail,
                refs=[action.entry_id, action.cause_id],
                inputs={"reverses": action.entry_id, "cause_type": action.cause_type},
            )
            raise

    # ---------------------------------------------------------------- vendor accounts (§5.3)

    def _account(self, account_id: str, principal: str, action: Action) -> VendorBankAccount:
        acct = self._vendors.get_account(account_id)
        if acct is None:
            self._violation(
                principal,
                action,
                "unknown_account",
                f"no vendor bank account {account_id}",
                refs=[account_id],
            )
        assert acct is not None
        return acct

    def _open_pending(
        self, action: OpenPendingAccountAction, principal: str, scope: Scope
    ) -> VendorBankAccount:
        self._require(
            scope.edit_vendor,
            principal,
            action,
            "edit_vendor_required",
            "opening a pending account needs edit_vendor scope",
            [action.vendor_id],
        )
        if self._vendors.get_vendor(action.vendor_id) is None:
            self._violation(
                principal,
                action,
                "unknown_vendor",
                f"no vendor {action.vendor_id}",
                refs=[action.vendor_id],
            )
        now = self._now()
        acct = VendorBankAccount(
            id=self._vendors.next_account_id(),
            vendor_id=action.vendor_id,
            iban_or_acct=action.iban_or_acct,
            account_hash=account_hash(action.iban_or_acct),
            status="pending",
            requested_at=now,
            requested_via=action.requested_via,
            requested_by=principal,
            request_event_id=action.request_event_id,
            verified_via=None,
            verified_event_id=None,
            verified_at=None,
            hold_until=now + timedelta(hours=action.hold_hours),
            approved_by=None,
            approval_event_id=None,
            approved_at=None,
            on_chip=False,
            retired_at=None,
        )
        self._vendors.insert_account(acct)
        log.info(
            "account_pending",
            principal=principal,
            account_id=acct.id,
            vendor_id=acct.vendor_id,
            event_id=action.request_event_id,
            rule_id="R8",
        )
        return acct

    def _mark_verified(
        self, action: MarkVerifiedAction, principal: str, scope: Scope
    ) -> VendorBankAccount:
        self._require(
            scope.edit_vendor,
            principal,
            action,
            "edit_vendor_required",
            "verification needs edit_vendor scope",
            [action.account_id],
        )
        acct = self._account(action.account_id, principal, action)
        if acct.status != "pending":
            self._violation(
                principal,
                action,
                "not_pending",
                f"account {acct.id} is {acct.status}, not pending",
                refs=[acct.id],
            )
        updated = acct.model_copy(
            update={
                "verified_via": action.verified_via,
                "verified_event_id": action.verified_event_id,
                "verified_at": self._now(),
            }
        )
        self._vendors.update_account(updated)
        log.info(
            "account_verified",
            principal=principal,
            account_id=acct.id,
            via=action.verified_via,
            event_id=action.verified_event_id,
            rule_id="R8",
        )
        return updated

    def _approve(
        self, action: ApproveAccountAction, principal: str, scope: Scope
    ) -> VendorBankAccount:
        self._require(
            scope.approve,
            principal,
            action,
            "approve_required",
            "approving an account needs approve scope",
            [action.account_id],
        )
        acct = self._account(action.account_id, principal, action)
        if acct.requested_by == principal:
            self._violation(
                principal,
                action,
                "requested_by_equals_approved_by",
                f"{principal!r} requested account {acct.id} and may not approve it",
                refs=[acct.id],
            )
        if acct.status != "pending":
            self._violation(
                principal,
                action,
                "not_pending",
                f"account {acct.id} is {acct.status}, not pending",
                refs=[acct.id],
            )
        updated = acct.model_copy(
            update={
                "approved_by": principal,
                "approval_event_id": action.approval_event_id,
                "approved_at": self._now(),
            }
        )
        self._vendors.update_account(updated)
        log.info(
            "account_approved",
            principal=principal,
            account_id=acct.id,
            event_id=action.approval_event_id,
            rule_id="R8",
        )
        return updated

    def _activate(
        self, action: ActivateIfReadyAction, principal: str, scope: Scope
    ) -> VendorBankAccount:
        self._require(
            scope.edit_vendor or scope.approve,
            principal,
            action,
            "edit_vendor_required",
            "activation needs edit_vendor scope",
            [action.account_id],
        )
        acct = self._account(action.account_id, principal, action)
        now = self._now()
        if acct.status != "pending":
            self._violation(
                principal,
                action,
                "not_pending",
                f"account {acct.id} is {acct.status}, not pending",
                refs=[acct.id],
            )
        if acct.verified_via is None:
            self._violation(
                principal,
                action,
                "not_verified",
                f"account {acct.id} has not been verified",
                refs=[acct.id],
            )
        if acct.approved_by is None:
            self._violation(
                principal,
                action,
                "not_approved",
                f"account {acct.id} has not been approved",
                refs=[acct.id],
            )
        if acct.verified_via == acct.requested_via:
            self._violation(
                principal,
                action,
                "same_channel",
                f"account {acct.id} was verified over the request channel ({acct.requested_via})",
                refs=[acct.id],
            )
        if now < acct.hold_until:
            self._violation(
                principal,
                action,
                "hold_not_elapsed",
                f"account {acct.id} is in hold until {acct.hold_until.isoformat()}",
                refs=[acct.id],
            )
        current = self._vendors.active_account(acct.vendor_id)
        if current is not None and current.id != acct.id:
            self._vendors.update_account(
                current.model_copy(update={"status": "retired", "retired_at": now})
            )
        activated = acct.model_copy(update={"status": "active"})
        self._vendors.update_account(activated)
        log.info(
            "account_activated",
            principal=principal,
            account_id=acct.id,
            vendor_id=acct.vendor_id,
            retired=current.id if current else None,
            event_id=acct.approval_event_id,
            rule_id="R8",
        )
        return activated

    def _suspend(
        self, action: SuspendAccountAction, principal: str, scope: Scope
    ) -> VendorBankAccount:
        self._require(
            scope.edit_vendor or scope.approve,
            principal,
            action,
            "edit_vendor_required",
            "suspension needs edit_vendor scope",
            [action.account_id],
        )
        acct = self._account(action.account_id, principal, action)
        if acct.status not in ("active", "pending"):
            self._violation(
                principal,
                action,
                "not_suspendable",
                f"account {acct.id} is {acct.status}",
                refs=[acct.id],
            )
        suspended = acct.model_copy(update={"status": "suspended"})
        self._vendors.update_account(suspended)
        log.info(
            "account_suspended",
            principal=principal,
            account_id=acct.id,
            vendor_id=acct.vendor_id,
            reason=action.reason,
            event_id=None,
            rule_id="R9",
        )
        return suspended

    def _restore_prior(
        self, action: RestorePriorAction, principal: str, scope: Scope
    ) -> VendorBankAccount:
        self._require(
            scope.edit_vendor or scope.approve,
            principal,
            action,
            "edit_vendor_required",
            "restoring an account needs edit_vendor scope",
            [action.vendor_id],
        )
        if self._vendors.active_account(action.vendor_id) is not None:
            self._violation(
                principal,
                action,
                "active_exists",
                f"vendor {action.vendor_id} already has an active account",
                refs=[action.vendor_id],
            )
        retired = [
            a
            for a in self._vendors.accounts(vendor_id=action.vendor_id, status="retired")
            if a.retired_at is not None
        ]
        if not retired:
            self._violation(
                principal,
                action,
                "no_prior_account",
                f"vendor {action.vendor_id} has no retired account to restore",
                refs=[action.vendor_id],
            )
        prior = max(retired, key=lambda a: (a.retired_at or datetime.min.replace(tzinfo=UTC), a.id))
        restored = prior.model_copy(update={"status": "active", "retired_at": None})
        self._vendors.update_account(restored)
        log.info(
            "account_restored",
            principal=principal,
            account_id=prior.id,
            vendor_id=action.vendor_id,
            event_id=None,
            rule_id="R9",
        )
        return restored

    def _request_payment(
        self, action: RequestPaymentAction, principal: str, scope: Scope
    ) -> VendorBankAccount:
        self._require(
            scope.request_payment,
            principal,
            action,
            "request_payment_required",
            "requesting a payment needs request_payment scope",
            [action.pay_id, action.vendor_id],
        )
        if action.amount_minor <= 0:
            self._violation(
                principal,
                action,
                "non_positive_amount",
                f"payment {action.pay_id} has amount {action.amount_minor}",
                refs=[action.pay_id],
            )
        acct = self._account(action.account_id, principal, action)
        if acct.vendor_id != action.vendor_id:
            self._violation(
                principal,
                action,
                "account_vendor_mismatch",
                f"account {acct.id} belongs to {acct.vendor_id}, not {action.vendor_id}",
                refs=[action.pay_id, acct.id, action.vendor_id],
            )
        if acct.status != "active":
            self._violation(
                principal,
                action,
                "account_not_active",
                f"account {acct.id} is {acct.status}; only active accounts may be paid",
                refs=[action.pay_id, acct.id],
            )
        log.info(
            "payment_request_allowed",
            principal=principal,
            pay_id=action.pay_id,
            vendor_id=action.vendor_id,
            account_id=acct.id,
            eur_equiv_minor=action.eur_equiv_minor,
            event_id=None,
            rule_id="R10" if action.eur_equiv_minor > BUTTON_THRESHOLD_MINOR else None,
        )
        return acct

    def _set_on_chip(
        self, action: SetOnChipAction, principal: str, scope: Scope
    ) -> VendorBankAccount:
        self._require(
            scope.edit_allowlist,
            principal,
            action,
            "edit_allowlist_required",
            "only the provisioner (edit_allowlist) may set on_chip",
            [action.account_id],
        )
        if not self._jumper():
            self._violation(
                principal,
                action,
                "jumper_absent",
                "provisioning needs the jumper present",
                refs=[action.account_id],
            )
        acct = self._account(action.account_id, principal, action)
        if (
            acct.status != "active"
            or acct.approved_by is None
            or acct.verified_via is None
            or acct.verified_via == acct.requested_via
        ):
            self._violation(
                principal,
                action,
                "not_active",
                f"account {acct.id} does not meet the §5.3 invariants for provisioning (status={acct.status})",
                refs=[acct.id],
            )
        provisioned = acct.model_copy(update={"on_chip": True})
        self._vendors.update_account(provisioned)
        log.info(
            "account_on_chip",
            principal=principal,
            account_id=acct.id,
            vendor_id=acct.vendor_id,
            event_id=None,
            rule_id=None,
        )
        return provisioned

    # ---------------------------------------------------------------- refusals are events

    def _require(
        self, ok: bool, principal: str, action: Action, reason: str, detail: str, refs: list[str]
    ) -> None:
        if not ok:
            self._violation(principal, action, reason, detail, refs=refs)

    def _violation(
        self, principal: str, action: Action, reason: str, detail: str, *, refs: list[str]
    ) -> NoReturn:
        exc = PolicyViolation(reason, detail)
        self._events.append(
            make_event(
                "agent_action",
                {
                    "agent": principal,
                    "action_type": "policy_violation",
                    "refs": refs,
                    "rule_id": None,
                    "inputs": {"action": type(action).__name__},
                    "decision": {"reason": reason, "detail": detail},
                    "explanation": str(exc),
                },
                event_time=self._now(),
                source="policy",
                actor=principal,
            )
        )
        log.warning(
            "policy_violation",
            principal=principal,
            action=type(action).__name__,
            reason=reason,
            detail=detail,
            event_id=None,
            rule_id=None,
        )
        raise exc

    def _log_post_rejected(
        self,
        principal: str,
        reason_code: str,
        detail: str,
        *,
        refs: list[str],
        inputs: dict[str, Any],
    ) -> None:
        result = self._events.append(
            make_event(
                "agent_action",
                {
                    "agent": principal,
                    "action_type": "post_rejected",
                    "refs": refs,
                    "rule_id": None,
                    "inputs": inputs,
                    "decision": {"reason_code": reason_code, "detail": detail},
                    "explanation": f"PostRejected({reason_code}): {detail}",
                },
                event_time=self._now(),
                source="policy",
                actor=principal,
            )
        )
        log.warning(
            "post_rejected",
            principal=principal,
            reason_code=reason_code,
            detail=detail,
            event_id=result.event.id,
            rule_id=None,
        )
