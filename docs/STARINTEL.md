# Optional StarIntel boundary

The source authority is [starintel-doc at the pinned SDK commit](https://github.com/lost-rob0t/starintel-doc/tree/6aa8051dc7ea560943a60a25e1d600467f2778bc),
version 0.10.1. Its [canonical schema lock](https://github.com/lost-rob0t/starintel-doc/blob/6aa8051dc7ea560943a60a25e1d600467f2778bc/schema/starintel-schema.lock.json)
pins StarLang commit `9198370f7a6f3e2a5ea00af3efdb6d705e102650` and 18 release-file
hashes. Generated release files are not vendored or edited here.

Verified exports from `starintel_doc/__init__.py` used by the adapter:
`SPEC_VERSION`, `build_http_transaction`, `validate_document`, `parse_json`,
and `stringify_json`. The canonical builder accepts dataset, method, URL,
response_status, optional response_headers, and observed_at, then produces and
validates the canonical flat lowerCamelCase 0.10.1 HTTP transaction.

The archive's SQLite tables and JSON capture manifests are operational state,
not StarIntel documents. Do not relabel them as canonical. Do not use historical
`starintel_doc.network_capture` builders for new canonical output. SDK loading,
validation, or mapping errors preserve original bytes and checkpoints.

This bootstrap only produces optional HTTP transaction metadata through the SDK.
It does not claim a complete article/event/election ontology mapping. Retain
source SHA-256 and capture ID joins, exact numeric parsing, and version identity
when extending normalization.

## License status

The public SDK's setup.py declares an MIT license classifier, but an explicit
root license grant was not verified at the pinned commit. This project references
it as an optional dependency; it does not copy, vendor, or relicense SDK code.
Verify upstream licensing before distributing bundled SDK builds. No private
actor implementation is used.
