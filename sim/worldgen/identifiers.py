"""Valid-by-construction identifiers: VAT IDs (python-stdnum), IBANs (schwifty), US ABA and
Japanese domestic account formats."""

from __future__ import annotations

import random
from dataclasses import dataclass

from schwifty import IBAN
from stdnum.ee import kmkr
from stdnum.eu import vat as eu_vat
from stdnum.jp import cn as jp_cn
from stdnum.us import ein as us_ein
from stdnum.us import rtn as us_rtn

from sim.models import BankAccount


def _digits(rng: random.Random, n: int, first_nonzero: bool = True) -> str:
    first = str(rng.randrange(1, 10)) if first_nonzero else str(rng.randrange(10))
    return first + "".join(str(rng.randrange(10)) for _ in range(n - 1))


def _brute_last_digit(rng: random.Random, prefix: str, body_len: int, is_valid) -> str:  # type: ignore[no-untyped-def]
    while True:
        body = _digits(rng, body_len - 1)
        for d in "0123456789":
            candidate = prefix + body + d
            if is_valid(candidate):
                return candidate


def gen_vat_id(rng: random.Random, country: str) -> str:
    if country == "DE":
        return _brute_last_digit(rng, "DE", 9, eu_vat.is_valid)
    if country == "CZ":
        return _brute_last_digit(rng, "CZ", 8, eu_vat.is_valid)
    if country == "PL":
        return _brute_last_digit(rng, "PL", 10, eu_vat.is_valid)
    if country == "AT":
        return _brute_last_digit(rng, "ATU", 8, eu_vat.is_valid)
    if country == "EE":
        return _brute_last_digit(rng, "EE", 9, kmkr.is_valid)
    if country == "JP":
        base = _digits(rng, 12)
        number = jp_cn.calc_check_digit(base) + base
        assert jp_cn.is_valid(number)
        return "T" + number
    if country == "US":
        while True:
            candidate = f"{rng.randrange(10, 100):02d}-{_digits(rng, 7, first_nonzero=False)}"
            if us_ein.is_valid(candidate):
                return candidate
    raise ValueError(f"no VAT id generator for {country}")


@dataclass(frozen=True)
class Bank:
    code: str
    name: str
    bic: str


BANKS: dict[str, list[Bank]] = {
    "DE": [
        Bank("37040044", "Commerzbank AG", "COBADEFFXXX"),
        Bank("50010517", "ING-DiBa AG", "INGDDEFFXXX"),
        Bank("70020270", "UniCredit Bank – HypoVereinsbank", "HYVEDEMMXXX"),
        Bank("10070000", "Deutsche Bank AG", "DEUTDEBBXXX"),
        Bank("76050101", "Sparkasse Nürnberg", "SSKNDE77XXX"),
        Bank("70150000", "Stadtsparkasse München", "SSKMDEMMXXX"),
    ],
    "CZ": [
        Bank("0100", "Komerční banka, a.s.", "KOMBCZPPXXX"),
        Bank("0300", "Československá obchodní banka, a. s.", "CEKOCZPP"),
        Bank("0800", "Česká spořitelna, a.s.", "GIBACZPX"),
        Bank("2010", "Fio banka, a.s.", "FIOBCZPPXXX"),
        Bank("5500", "Raiffeisenbank a.s.", "RZBCCZPP"),
        Bank("6210", "mBank S.A., organizační složka", "BREXCZPPXXX"),
    ],
    "PL": [
        Bank("11402004", "mBank S.A.", "BREXPLPWMBK"),
        Bank("10501214", "ING Bank Śląski S.A.", "INGBPLPW"),
        Bank("12401037", "Bank Polska Kasa Opieki S.A.", "PKOPPLPW"),
        Bank("10202313", "PKO Bank Polski S.A.", "BPKOPLPW"),
        Bank("16001462", "BNP Paribas Bank Polska S.A.", "PPABPLPK"),
    ],
    "AT": [
        Bank("12000", "UniCredit Bank Austria AG", "BKAUATWWXXX"),
        Bank("20111", "Erste Bank der oesterreichischen Sparkassen AG", "GIBAATWWXXX"),
        Bank("14000", "BAWAG P.S.K.", "BAWAATWWXXX"),
        Bank("32000", "Raiffeisenlandesbank Niederösterreich-Wien AG", "RLNWATWWXXX"),
    ],
    "EE": [Bank("22", "Swedbank AS", "HABAEE2X"), Bank("10", "SEB Pank AS", "EEUHEE2X")],
}

US_BANKS: list[Bank] = [
    Bank("072000326", "Comerica Bank", "MNBDUS33"),
    Bank("021000021", "JPMorgan Chase Bank, N.A.", "CHASUS33"),
    Bank("026009593", "Bank of America, N.A.", "BOFAUS3N"),
    Bank("121000248", "Wells Fargo Bank, N.A.", "WFBIUS6S"),
    Bank("072000096", "Fifth Third Bank", "FTBCUS3C"),
]

JP_BANKS: list[Bank] = [
    Bank("0001", "みずほ銀行 (Mizuho Bank)", "MHCBJPJT"),
    Bank("0005", "三菱UFJ銀行 (MUFG Bank)", "BOTKJPJT"),
    Bank("0009", "三井住友銀行 (SMBC)", "SMBCJPJT"),
    Bank("0138", "横浜銀行 (Bank of Yokohama)", "HAMAJPJT"),
]


def gen_iban_account(
    rng: random.Random, country: str, exclude_bank: str | None = None
) -> BankAccount:
    """A random IBAN at one of the listed banks; schwifty solves the national checksum."""
    banks = [b for b in BANKS[country] if b.code != exclude_bank]
    bank = rng.choice(banks)
    kwargs: dict[str, str] = {"bank_code": bank.code}
    if country == "CZ":
        kwargs["branch_code"] = "000000"
    if country == "EE":
        kwargs["branch_code"] = "00"
    iban = IBAN.random(country, random=rng, **kwargs)
    iban.validate(validate_bban=True)
    return BankAccount(
        kind="iban",
        iban=str(iban),
        bic=iban.bic.compact if iban.bic is not None else bank.bic,
        bank_name=iban.bank_name or bank.name,
        account_number=None,
        routing_number=None,
        bank_code=bank.code,
        branch_code=None,
    )


def gen_us_account(rng: random.Random) -> BankAccount:
    bank = rng.choice(US_BANKS)
    assert us_rtn.is_valid(bank.code)
    return BankAccount(
        kind="us_aba",
        iban=None,
        bic=bank.bic,
        bank_name=bank.name,
        account_number=_digits(rng, 10),
        routing_number=bank.code,
        bank_code=None,
        branch_code=None,
    )


def gen_jp_account(rng: random.Random) -> BankAccount:
    bank = rng.choice(JP_BANKS)
    return BankAccount(
        kind="jp_domestic",
        iban=None,
        bic=bank.bic,
        bank_name=bank.name,
        account_number=_digits(rng, 7),
        routing_number=None,
        bank_code=bank.code,
        branch_code=_digits(rng, 3, first_nonzero=False),
    )
