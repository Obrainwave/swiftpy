from __future__ import annotations

import html
import re
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any

from swiftpy.ai.vectorstore.base import SearchResult

_CONTEXT_BOUNDARY_RE = re.compile(
    r"-{3,}\s*(BEGIN|END)\s+UNTRUSTED\s+REFERENCE\s+CONTEXT\s*-{3,}",
    re.IGNORECASE,
)
_NONCE_BOUNDARY_RE = re.compile(
    r"<<<\s*(?:END\s+)?CONTEXT_[A-Za-z0-9_-]*>>>",
    re.IGNORECASE,
)
_XML_TAG_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


def escape_context_boundaries(text: str) -> str:
    """Neutralize embedded context boundary delimiters in untrusted text."""
    escaped = _CONTEXT_BOUNDARY_RE.sub(
        lambda m: f"[escaped: {m.group(1).upper()} UNTRUSTED REFERENCE CONTEXT]",
        text,
    )
    return _NONCE_BOUNDARY_RE.sub("[escaped: CONTEXT BOUNDARY]", escaped)


def _extract_doc_content(
    doc: SearchResult | str | dict[str, Any],
    index: int,
) -> tuple[str, str, dict[str, Any], float]:
    """
    Extract (content, id, metadata, score) from a document input.
    """
    if isinstance(doc, SearchResult):
        return doc.content, doc.id, doc.metadata, doc.score
    if isinstance(doc, str):
        return doc, f"doc_{index}", {}, 0.0
    if isinstance(doc, dict):
        return (
            str(doc.get("content", "")),
            str(doc.get("id", f"doc_{index}")),
            dict(doc.get("metadata") or {}),
            float(doc.get("score", 0.0)),
        )
    raise TypeError(f"Unsupported document type at index {index}: {type(doc).__name__}")


class ContextFormatter(ABC):
    """
    Abstract contract for context formatting strategies in RAG pipelines.
    """

    @abstractmethod
    def format(
        self,
        documents: Sequence[SearchResult | str | dict[str, Any]],
    ) -> str:
        """
        Format a sequence of retrieved chunks into a prompt-ready context string.
        """

    @classmethod
    def create(cls, strategy: str | ContextFormatter) -> ContextFormatter:
        """
        Factory method resolving a formatter instance from a strategy name or instance.

        Supported strategies:
        - 'numbered', 'numbered_list' -> NumberedListFormatter
        - 'xml', 'xml_blocks' -> XMLBlocksFormatter
        - 'plain', 'concatenation', 'plain_concatenation' -> PlainConcatenationFormatter
        """
        if isinstance(strategy, ContextFormatter):
            return strategy

        norm = str(strategy).lower().strip().replace("-", "_")
        if norm in ("numbered", "numbered_list", "number"):
            return NumberedListFormatter()
        if norm in ("xml", "xml_blocks", "xml_block"):
            return XMLBlocksFormatter()
        if norm in ("plain", "concatenation", "plain_concatenation", "concat"):
            return PlainConcatenationFormatter()

        raise ValueError(
            f"Unknown context formatter strategy '{strategy}'. "
            f"Supported strategies: 'numbered', 'xml', 'concatenation'."
        )

    @classmethod
    def numbered(
        cls,
        separator: str = "\n\n",
        include_metadata: bool = False,
    ) -> NumberedListFormatter:
        """Create a numbered list context formatter."""
        return NumberedListFormatter(separator=separator, include_metadata=include_metadata)

    @classmethod
    def xml(
        cls,
        root_tag: str = "context",
        document_tag: str = "document",
        include_attributes: bool = True,
        escape_content: bool = True,
    ) -> XMLBlocksFormatter:
        """Create an XML blocks context formatter."""
        return XMLBlocksFormatter(
            root_tag=root_tag,
            document_tag=document_tag,
            include_attributes=include_attributes,
            escape_content=escape_content,
        )

    @classmethod
    def concatenation(
        cls,
        separator: str = "\n\n",
        prefix: str = "",
        suffix: str = "",
    ) -> PlainConcatenationFormatter:
        """Create a plain concatenation context formatter."""
        return PlainConcatenationFormatter(separator=separator, prefix=prefix, suffix=suffix)


class NumberedListFormatter(ContextFormatter):
    """
    Formats retrieved documents as a numbered list:
    [1] First document content
    [2] Second document content
    """

    def __init__(
        self,
        separator: str = "\n\n",
        include_metadata: bool = False,
    ) -> None:
        self.separator = separator
        self.include_metadata = include_metadata

    def format(
        self,
        documents: Sequence[SearchResult | str | dict[str, Any]],
    ) -> str:
        if not documents:
            return ""

        blocks: list[str] = []
        for idx, doc in enumerate(documents, start=1):
            content, doc_id, metadata, _ = _extract_doc_content(doc, idx)
            safe_content = escape_context_boundaries(content)
            header = f"[{idx}]"
            if self.include_metadata:
                meta_parts = [f"id: {escape_context_boundaries(str(doc_id))}"]
                source = metadata.get("source") or metadata.get("title")
                if source:
                    meta_parts.append(
                        f"source: {escape_context_boundaries(str(source))}"
                    )
                header += f" ({', '.join(meta_parts)})"

            blocks.append(f"{header} {safe_content}")

        return self.separator.join(blocks)


class XMLBlocksFormatter(ContextFormatter):
    """
    Formats retrieved documents into structured XML blocks:
    <context>
      <document index="1" id="doc_1">
        Content of document 1
      </document>
    </context>
    """

    def __init__(
        self,
        root_tag: str = "context",
        document_tag: str = "document",
        include_attributes: bool = True,
        escape_content: bool = True,
    ) -> None:
        if not isinstance(root_tag, str) or not _XML_TAG_NAME_RE.match(root_tag):
            raise ValueError(
                f"Invalid XML root_tag {root_tag!r}; expected match for {_XML_TAG_NAME_RE.pattern}"
            )
        if not isinstance(document_tag, str) or not _XML_TAG_NAME_RE.match(document_tag):
            raise ValueError(
                f"Invalid XML document_tag {document_tag!r}; expected match for {_XML_TAG_NAME_RE.pattern}"
            )
        self.root_tag = root_tag
        self.document_tag = document_tag
        self.include_attributes = include_attributes
        self.escape_content = escape_content

    def format(
        self,
        documents: Sequence[SearchResult | str | dict[str, Any]],
    ) -> str:
        if not documents:
            return f"<{self.root_tag}></{self.root_tag}>"

        lines: list[str] = [f"<{self.root_tag}>"]
        for idx, doc in enumerate(documents, start=1):
            content, doc_id, metadata, _score = _extract_doc_content(doc, idx)
            attrs = ""
            if self.include_attributes:
                safe_id = html.escape(str(doc_id), quote=True)
                attrs = f' index="{idx}" id="{safe_id}"'
                source = metadata.get("source") or metadata.get("title")
                if source:
                    safe_source = html.escape(str(source), quote=True)
                    attrs += f' source="{safe_source}"'

            lines.append(f"  <{self.document_tag}{attrs}>")
            # Indent each line of content (preserving trailing newlines) and escape untrusted markup
            safe_content = (
                content if self.escape_content else escape_context_boundaries(content)
            )
            for content_line in safe_content.split("\n"):
                formatted_line = (
                    html.escape(escape_context_boundaries(content_line))
                    if self.escape_content
                    else content_line
                )
                lines.append(f"    {formatted_line}")
            lines.append(f"  </{self.document_tag}>")

        lines.append(f"</{self.root_tag}>")
        return "\n".join(lines)


class PlainConcatenationFormatter(ContextFormatter):
    """
    Formats retrieved documents by joining their raw text with a separator.
    """

    def __init__(
        self,
        separator: str = "\n\n",
        prefix: str = "",
        suffix: str = "",
    ) -> None:
        self.separator = separator
        self.prefix = prefix
        self.suffix = suffix

    def format(
        self,
        documents: Sequence[SearchResult | str | dict[str, Any]],
    ) -> str:
        if not documents:
            return ""

        chunks = [
            escape_context_boundaries(_extract_doc_content(doc, idx)[0])
            for idx, doc in enumerate(documents, start=1)
        ]
        joined = self.separator.join(chunks)
        return f"{self.prefix}{joined}{self.suffix}"
