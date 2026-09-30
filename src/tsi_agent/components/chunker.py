"""Composant Haystack : fichiers extraits (ByteStream) -> morceaux (Document). Portable dans deepset."""

import yaml
from haystack import Document, component
from haystack.dataclasses import ByteStream

from tsi_agent import runlog
from tsi_agent.chunking import chunk_file


@component
class ProjectChunker:
    @component.output_types(documents=list[Document], skipped=list[str])
    def run(self, sources: list[ByteStream]) -> dict[str, list]:
        documents: list[Document] = []
        skipped: list[str] = []
        for src in sources:
            # En local : chemin relatif pose par ProjectFileLoader ; dans deepset : nom du fichier de l'espace.
            rel = str(src.meta.get("file_path") or src.meta.get("file_name") or "")
            try:
                chunks = chunk_file(rel, src.data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError, yaml.YAMLError) as exc:
                runlog.event("ProjectChunker", "alert", rel, "warning", message=f"cannot chunk {rel}: {exc}")
                skipped.append(rel)
                continue
            runlog.event("ProjectChunker", "chunk", rel, chunks=len(chunks),
                         words=sum(len((d.content or "").split()) for d in chunks))
            documents.extend(chunks)
        return {"documents": documents, "skipped": skipped}
