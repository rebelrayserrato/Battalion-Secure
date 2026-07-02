# Architecture

The application follows a local pipeline:

`original files → source chunks → SQLite + Chroma → rule/data reviews → findings guardrail → reports`

`ReviewService` coordinates the workflow. Extraction modules create immutable
`SourceChunk` records with stable source references. Review modules produce candidates;
only `finalize_findings` may turn them into persisted findings, and it rejects candidates
without source chunks. The optional Ollama connector receives persisted findings only.
