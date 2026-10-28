from __future__ import annotations

import pytest

from receipt_bench.projects.models import Project
from receipt_bench.projects.prompting import render_user_prompt


def test_project_prompts_are_dataset_specific(
    sroie_project: Project, cord_project: Project
) -> None:
    sroie_direct = sroie_project.prompt("direct-multimodal")
    cord_direct = cord_project.prompt("direct-multimodal")

    assert "company, address, date, and total" in sroie_direct.system_instruction
    assert "menu as an array" in cord_direct.system_instruction
    assert "generic receipt schema" in cord_direct.system_instruction
    assert sroie_direct.prompt_id != cord_direct.prompt_id


def test_direct_and_ocr_templates_render_separately(sroie_project: Project) -> None:
    direct = render_user_prompt(
        sroie_project.prompt("direct-multimodal"),
        sroie_project.project_schema,
    )
    normalizer = render_user_prompt(
        sroie_project.prompt("ocr-normalizer"),
        sroie_project.project_schema,
        ocr_text="EXAMPLE STORE\nTOTAL 9.00",
    )

    assert '"company"' in direct
    assert "OCR evidence begins" not in direct
    assert "EXAMPLE STORE\nTOTAL 9.00" in normalizer
    with pytest.raises(ValueError, match="require non-empty OCR"):
        render_user_prompt(
            sroie_project.prompt("ocr-normalizer"),
            sroie_project.project_schema,
            ocr_text=" ",
        )
