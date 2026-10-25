"""R15 must recognise every retention clause the world prints, in every language it prints it in.
The real-API world run missed the Polish "Kaucja gwarancyjna" until this test existed."""

from __future__ import annotations

import pytest

from agents.validate import retention_bp_from
from sim.worldgen.materialize import RETENTION_NOTE


@pytest.mark.parametrize("language", sorted(RETENTION_NOTE))
def test_every_world_retention_note_is_recognised(language: str) -> None:
    assert retention_bp_from(RETENTION_NOTE[language]) == 1000, language


def test_retention_is_read_from_notes_or_terms_and_absent_otherwise() -> None:
    assert retention_bp_from(None, "Kaucja gwarancyjna 10 % wartości faktury") == 1000
    assert retention_bp_from("Sposób zapłaty: przelew. Odwrotne obciążenie.", "45 dni") is None
    assert retention_bp_from("Retention 5 % until acceptance") == 500
