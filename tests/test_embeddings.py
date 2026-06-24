from __future__ import annotations

import math

from raglex.embeddings import HashingEmbeddingProvider, chunk_document
from raglex.embeddings.chunking import ChunkConfig


# -- provider ---------------------------------------------------------------
def test_hashing_provider_deterministic_and_normalised():
    p = HashingEmbeddingProvider(dimensions=128)
    v1 = p.embed(["data protection erasure"])[0]
    v2 = p.embed(["data protection erasure"])[0]
    assert v1 == v2  # deterministic
    assert len(v1) == 128
    assert abs(math.sqrt(sum(x * x for x in v1)) - 1.0) < 1e-9  # L2-normalised


def test_hashing_provider_shared_terms_more_similar():
    p = HashingEmbeddingProvider(dimensions=512)
    a = p.embed(["the right to erasure of personal data"])[0]
    b = p.embed(["erasure of personal data is a right"])[0]
    c = p.embed(["merger control and competition remedies"])[0]
    cos = lambda x, y: sum(i * j for i, j in zip(x, y))
    assert cos(a, b) > cos(a, c)  # overlapping vocabulary ranks higher


def test_provider_family_is_comparability_key():
    p = HashingEmbeddingProvider(dimensions=64)
    assert p.family == ("local-hashing", "hashing-bow", "v1", 64)


# -- chunker ----------------------------------------------------------------
def test_chunk_on_paragraph_units_with_offsets():
    text = "First paragraph about data.\n\nSecond paragraph about erasure rights here."
    chunks = chunk_document("d1", text, config=ChunkConfig(min_tokens=1, max_tokens=100))
    assert len(chunks) == 2
    # char spans map back into the source text exactly (§6b.5)
    for c in chunks:
        assert text[c.char_start:c.char_end].strip() == c.text


def test_contextual_header_in_embed_input_only():
    chunks = chunk_document(
        "d1", "A paragraph about personal data.",
        meta={"source": "nl", "court": "Hoge Raad", "year": "2024", "tags": ["data_protection"]},
        config=ChunkConfig(min_tokens=1),
    )
    c = chunks[0]
    assert c.embed_input.startswith("[nl · Hoge Raad · 2024 · data_protection")
    assert "Hoge Raad" not in c.text  # header not polluting stored display text


def test_oversized_unit_split_at_sentence_boundaries():
    para = " ".join(f"Sentence number {i} about the matter." for i in range(20))
    chunks = chunk_document("d1", para, config=ChunkConfig(min_tokens=4, target_tokens=12, max_tokens=20))
    assert len(chunks) > 1  # one big paragraph split into several chunks
    assert all(len(c.text.split()) <= 40 for c in chunks)


def test_sentence_splitter_respects_legal_abbreviations():
    from raglex.embeddings.chunking import _split_sentences

    sents = _split_sentences("See art. 22 GDPR. The court agreed.")
    # must not split on 'art.'; should split on the real sentence end
    assert any("art. 22 GDPR" in s for s in sents)
    assert len(sents) == 2


def test_empty_text_yields_no_chunks():
    assert chunk_document("d1", "") == []


def test_chunks_on_adapter_segments_when_provided():
    from raglex.core.models import Segment

    # flat text with two native units the adapter identified (not blank-line split)
    text = "The court considered Article 17. It then turned to the facts of the case."
    segments = [
        Segment(label="[1]", char_start=0, char_end=31, kind="paragraph"),
        Segment(label="[2]", char_start=32, char_end=len(text), kind="paragraph"),
    ]
    chunks = chunk_document("d1", text, segments=segments, config=ChunkConfig(min_tokens=1, max_tokens=100))
    # the citable labels survive as the chunk's structural_unit, mapping to spans
    units = {c.structural_unit for c in chunks}
    assert "[1]" in units or "[2]" in units
    for c in chunks:
        assert text[c.char_start:c.char_end].strip()


def test_segment_label_appears_in_embedding_header():
    from raglex.core.models import Segment

    text = "Motivations of the Court on data minimisation and proportionality here."
    seg = [Segment(label="motivations", char_start=0, char_end=len(text), kind="zone")]
    c = chunk_document("d1", text, segments=seg, meta={"court": "CJEU"}, config=ChunkConfig(min_tokens=1))[0]
    assert "motivations" in c.embed_input  # the citable unit pulls the vector (§6b.4)
    assert "motivations" not in c.text  # header only in embedding input
