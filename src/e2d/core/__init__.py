"""Shared conversion core: a small DQL builder plus dialect-neutral IR for
boolean filters and aggregation trees.

Front-ends translate *into* this IR; one emitter owns DQL syntax:

* Query DSL JSON  -> ``query_dsl.parse_query`` / ``parse_aggs``
* Lucene          -> ``lucene.translate_lucene``
* KQL             -> ``dashboards.kql.parse_kql``
* Lens columns    -> ``AggTree``
* Watcher searches, SLO countIf, search-source filters reuse the same nodes

ES|QL is a pipeline language (commands + expressions), so it has its own
expression AST and then the same DQL linter. Logstash conditions are a separate
ingest dialect (OpenPipeline DQL/DPL), not this filter IR.
"""
