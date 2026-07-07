"""Retrieval memory — embedded per-project test-case knowledge base (optional).

Everything under this package is gated behind ``Config.rag_enabled`` (default
OFF). The default pipeline never imports it, so a run without the flag stays
byte-identical to a build without this package. RETRIEVAL_MEMORY_PLAN.md is the
implementation contract; the design narrative lives in docs/ARCHITECTURE.md
("Planned: retrieval memory").
"""
