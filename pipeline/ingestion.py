from __future__ import annotations

from pathlib import Path

from domain.models import ParagraphUnit, ScriptDocument
from legacy_core.ingestion import ingest_script_docx


class ScriptIngestionService:
    def ingest(self, file_path: str | Path) -> ScriptDocument:
        result = ingest_script_docx(file_path)
        issues_by_original_index: dict[int, list[str]] = {}
        issues_by_paragraph_no: dict[int, list[str]] = {}

        for issue in result.issues:
            if issue.original_index is not None:
                issues_by_original_index.setdefault(issue.original_index, []).append(issue.message)
            if issue.paragraph_no is not None:
                issues_by_paragraph_no.setdefault(issue.paragraph_no, []).append(issue.message)

        paragraphs = [
            ParagraphUnit(
                paragraph_no=paragraph.paragraph_no,
                original_index=paragraph.original_index,
                text=paragraph.text,
                numbering_valid=paragraph.numbering_valid,
                validation_issues=list(
                    dict.fromkeys(
                        issues_by_original_index.get(paragraph.original_index, [])
                        + issues_by_paragraph_no.get(paragraph.paragraph_no, [])
                    )
                ),
            )
            for paragraph in result.paragraphs
        ]
        issues = [issue.message for issue in result.issues]
        return ScriptDocument(
            source_path=result.source_file,
            header_text=result.header_text,
            paragraphs=paragraphs,
            numbering_issues=issues,
        )
