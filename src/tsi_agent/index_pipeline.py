"""Pipeline Haystack d'indexation (couche vectorisee), exportable en YAML pour un index deepset."""

from typing import Any

from haystack import Pipeline

from tsi_agent.components import ProjectChunker, ProjectFileLoader, VectorLayerWriter
from tsi_agent.config import Settings


def default_embedder(settings: Settings) -> Any:
    """Embedder local gratuit. Le prefixe `passage: ` est exige par les modeles e5 (requetes : `query: `)."""
    try:
        from haystack_integrations.components.embedders.sentence_transformers import \
            SentenceTransformersDocumentEmbedder
    except ImportError as exc:
        raise RuntimeError('embedder local absent : installer avec pip install -e ".[local]"') from exc
    return SentenceTransformersDocumentEmbedder(model=settings.embedding_model, prefix="passage: ",
                                                normalize_embeddings=True, meta_fields_to_embed=["context_header"],
                                                batch_size=settings.embedding_batch_size, progress_bar=False)


def embedder_id(embedder: Any) -> str:
    return f"{type(embedder).__name__}:{getattr(embedder, 'model', 'unknown')}"


def build_index_pipeline(settings: Settings, embedder: Any = None) -> Pipeline:
    embedder = embedder if embedder is not None else default_embedder(settings)
    eid = embedder_id(embedder)
    p = Pipeline()
    p.add_component("loader", ProjectFileLoader(output_dir=settings.output_dir, vectors_dir=settings.vectors_dir,
                                                embedder_id=eid))
    p.add_component("chunker", ProjectChunker())
    p.add_component("embedder", embedder)
    p.add_component("vector_writer", VectorLayerWriter(vectors_dir=settings.vectors_dir, embedder_id=eid,
                                                       pricing=settings.pricing))
    p.connect("loader.sources", "chunker.sources")
    p.connect("chunker.documents", "embedder.documents")
    p.connect("embedder.documents", "vector_writer.documents")
    p.connect("loader.sources", "vector_writer.sources")
    p.connect("loader.removed", "vector_writer.removed")
    p.connect("loader.full", "vector_writer.full")
    p.connect("chunker.skipped", "vector_writer.skipped")
    # OpenAI et Mock renvoient l'usage (tokens) ; sentence-transformers n'a pas de sortie `meta`.
    if hasattr(embedder.__haystack_output__, "meta"):
        p.connect("embedder.meta", "vector_writer.embed_meta")
    return p
