"""Daily MT940 statements for the three accounts (spec §2.7): opening balances, monthly fees,
one batch payment covering three invoices, an FX conversion line, payroll/tax/customer lines
shaped so the EUR balance sits near EUR 150k when T9 fires."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal

from core.bootstrap import OPENING_BANK_MINOR as OPENING_MINOR
from sim.fx import FxTable, rate_for
from sim.mt940 import Statement, StatementLine, truncate_remittance
from sim.worldgen.catalog import BANK_ACCOUNTS
from sim.worldgen.plan import END, START


@dataclass(frozen=True)
class ScriptedLine:
    currency: str
    day: date
    dc: str
    amount_minor: int
    txn_type: str
    customer_ref: str
    remittance: str


def scripted_lines(fx: FxTable, prepayment_minor: int, prepayment_po: str) -> list[ScriptedLine]:
    fx_rate = rate_for(fx, "CZK", date(2026, 10, 8))
    czk_amount = int((Decimal(200_000_00) * fx_rate).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
    L = ScriptedLine
    return [
        L("EUR", date(2026, 10, 1), "D", 4500, "NCHG", "NONREF", "ACCOUNT MAINTENANCE FEE 09/2026"),
        L(
            "EUR",
            date(2026, 10, 2),
            "D",
            18_743_012,
            "NTRF",
            "PAY-BATCH-0001",
            "/REMI/KMF/2026/09/0412 KMF/2026/09/0413 KMF/2026/09/0417 KATOWICE METAL FORMING S.A. SEPTEMBER 2026 BATCH OF THREE INVOICES, SEE REMITTANCE ADVICE PAY-BATCH-0001 FOR LINE DETAIL",
        ),
        L(
            "EUR",
            date(2026, 10, 3),
            "D",
            prepayment_minor,
            "NTRF",
            "PREPAY-0003",
            f"/REMI/PREPAYMENT 30PCT {prepayment_po} TL-7001 PRAEZISIONSWERKZEUGE HAHN GMBH",
        ),
        L(
            "EUR",
            date(2026, 10, 6),
            "C",
            42_000_000,
            "NTRF",
            "NONREF",
            "/ORDP/TOYOTA MOTOR EUROPE NV/REMI/SETTLEMENT 2026-09 PLANT MUNICH",
        ),
        L(
            "EUR",
            date(2026, 10, 8),
            "D",
            20_000_000,
            "NFEX",
            "FX-2026-1008",
            f"/REMI/FX CONVERSION EUR/CZK RATE {fx_rate} VALUE 2026-10-08 TO CZ4701000000190000259317",
        ),
        L(
            "CZK",
            date(2026, 10, 8),
            "C",
            czk_amount,
            "NFEX",
            "FX-2026-1008",
            f"/REMI/FX CONVERSION EUR/CZK RATE {fx_rate} EUR 200000,00 FROM DE44500105175407324931",
        ),
        L(
            "EUR",
            date(2026, 10, 10),
            "D",
            31_000_000,
            "NTRF",
            "UST-0926",
            "/REMI/UMSATZSTEUER 09/2026 FINANZAMT MUENCHEN STNR 143/123/45678",
        ),
        L(
            "EUR",
            date(2026, 10, 15),
            "C",
            38_000_000,
            "NTRF",
            "NONREF",
            "/ORDP/TOYOTA MOTOR EUROPE NV/REMI/SETTLEMENT 2026-09 PART 2",
        ),
        L(
            "EUR",
            date(2026, 10, 20),
            "D",
            124_000_000,
            "NTRF",
            "PAYROLL-1026",
            "/REMI/PAYROLL 10/2026 TBEU MUNICH LIBEREC GLIWICE EXPAT ACCOUNTS",
        ),
        L(
            "EUR",
            date(2026, 10, 22),
            "D",
            66_000_000,
            "NTRF",
            "ICO-SWEEP-1022",
            "/REMI/INTERCOMPANY SWEEP TBJ TREASURY NAGOYA OCTOBER",
        ),
        L(
            "EUR",
            date(2026, 10, 27),
            "C",
            150_000_000,
            "NTRF",
            "NONREF",
            "/ORDP/TOYOTA MOTOR EUROPE NV/REMI/SETTLEMENT 2026-10 ADVANCE",
        ),
        L("EUR", date(2026, 11, 1), "D", 4500, "NCHG", "NONREF", "ACCOUNT MAINTENANCE FEE 10/2026"),
        L(
            "EUR",
            date(2026, 11, 5),
            "C",
            64_000_000,
            "NTRF",
            "NONREF",
            "/ORDP/TOYOTA MOTOR EUROPE NV/REMI/SETTLEMENT 2026-10",
        ),
        L(
            "EUR",
            date(2026, 11, 10),
            "D",
            29_000_000,
            "NTRF",
            "UST-1026",
            "/REMI/UMSATZSTEUER 10/2026 FINANZAMT MUENCHEN",
        ),
        L(
            "CZK",
            date(2026, 10, 1),
            "D",
            65_000,
            "NCHG",
            "NONREF",
            "POPLATEK ZA VEDENI UCTU 09/2026",
        ),
        L(
            "CZK",
            date(2026, 10, 14),
            "D",
            185_000_000,
            "NTRF",
            "MZDY-1026",
            "/REMI/MZDY 10/2026 ZAVOD LIBEREC",
        ),
        L(
            "CZK",
            date(2026, 10, 21),
            "C",
            240_000_000,
            "NTRF",
            "NONREF",
            "/ORDP/TOYOTA BOSHOKU EUROPE GMBH/REMI/INTERCOMPANY FUNDING CZK",
        ),
        L(
            "CZK",
            date(2026, 11, 1),
            "D",
            65_000,
            "NCHG",
            "NONREF",
            "POPLATEK ZA VEDENI UCTU 10/2026",
        ),
        L(
            "CZK",
            date(2026, 11, 6),
            "D",
            41_230_000,
            "NTRF",
            "DPPO-Z4",
            "/REMI/ZALOHA DAN Z PRIJMU PO Q4 FU LIBEREC",
        ),
        L(
            "PLN",
            date(2026, 10, 1),
            "D",
            12_000,
            "NCHG",
            "NONREF",
            "OPLATA ZA PROWADZENIE RACHUNKU 09/2026",
        ),
        L(
            "PLN",
            date(2026, 10, 12),
            "D",
            64_000_000,
            "NTRF",
            "WYN-1026",
            "/REMI/WYNAGRODZENIA 10/2026 ZAKLAD GLIWICE",
        ),
        L(
            "PLN",
            date(2026, 10, 19),
            "C",
            90_000_000,
            "NTRF",
            "NONREF",
            "/ORDP/TOYOTA BOSHOKU EUROPE GMBH/REMI/INTERCOMPANY FUNDING PLN",
        ),
        L(
            "PLN",
            date(2026, 11, 1),
            "D",
            12_000,
            "NCHG",
            "NONREF",
            "OPLATA ZA PROWADZENIE RACHUNKU 10/2026",
        ),
        L(
            "PLN",
            date(2026, 11, 9),
            "D",
            11_840_000,
            "NTRF",
            "ZUS-1026",
            "/REMI/ZUS SKLADKI 10/2026 PLATNIK 6312665570",
        ),
    ]


def build_statements(
    rng: random.Random, fx: FxTable, prepayment_minor: int, prepayment_po: str
) -> list[Statement]:
    lines = scripted_lines(fx, prepayment_minor, prepayment_po)
    # a few ambient small credits (supplier refunds, interest) to keep statements lively
    kinds = [
        ("C", "NMSC", "/REMI/SUPPLIER REFUND CREDIT NOTE {n}/2026"),
        ("C", "NTRF", "/ORDP/TOYOTA MOTOR MANUFACTURING FRANCE/REMI/SERVICE PARTS SETTLEMENT {n}"),
        ("D", "NCHG", "CARD SETTLEMENT CORPORATE CARDS {n}"),
        ("D", "NMSC", "/REMI/SEPA DIRECT DEBIT ENERGY SUPPLY CONTRACT {n}"),
        ("C", "NMSC", "/REMI/INTEREST CREDIT {n}"),
    ]
    for _ in range(30):
        day = START + timedelta(days=rng.randrange(2, 44))
        ccy = rng.choice(["EUR", "EUR", "EUR", "CZK", "PLN"])
        dc, txn, template = rng.choice(kinds)
        amount = rng.randrange(120, 9800) * 10 if dc == "D" else rng.randrange(1200, 98000) * 10
        lines.append(
            ScriptedLine(
                ccy,
                day,
                dc,
                amount,
                txn,
                f"REF{rng.randrange(100000, 999999)}",
                template.format(n=rng.randrange(100, 999)),
            )
        )
    statements: list[Statement] = []
    for ccy in ("EUR", "CZK", "PLN"):
        balance = OPENING_MINOR[ccy]
        iban = BANK_ACCOUNTS[ccy]["iban"]
        for n in range((END - START).days + 1):
            day = START + timedelta(days=n)
            todays = [ln for ln in lines if ln.currency == ccy and ln.day == day]
            todays.sort(key=lambda ln: (ln.txn_type != "NCHG", ln.customer_ref, ln.amount_minor))
            stmt_lines = []
            for k, ln in enumerate(todays, start=1):
                stmt_lines.append(
                    StatementLine(
                        value_date=day,
                        entry_date=day,
                        dc=ln.dc,
                        amount_minor=ln.amount_minor,
                        txn_type=ln.txn_type,  # type: ignore[arg-type]
                        customer_ref=ln.customer_ref,
                        bank_ref=f"{ccy[0]}{day:%y%m%d}{k:03d}",
                        remittance=truncate_remittance(ln.remittance),
                    )
                )
            closing = balance + sum(
                ln.amount_minor if ln.dc == "C" else -ln.amount_minor for ln in stmt_lines
            )
            statements.append(
                Statement(
                    statement_id=f"{ccy}{day:%y%m%d}",
                    account_iban=iban,
                    currency=ccy,
                    statement_date=day,
                    sequence_no=n + 1,
                    opening_balance_minor=balance,
                    lines=stmt_lines,
                    closing_balance_minor=closing,
                )
            )
            balance = closing
    return statements
