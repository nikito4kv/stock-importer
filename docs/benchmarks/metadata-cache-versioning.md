# Metadata Cache Versioning

- Bump `METADATA_CACHE_KEY_VERSION` on any change to keyword normalization,
  prefilter rules, ranking weights, or other logic that affects
  `assess_candidate_quality()`.
- The metadata key currently captures the assessment inputs that can vary per
  candidate: provider id, URL, normalized keyword/query, referrer URL, author,
  license name, attribution flag, provider group, provider priority, and the
  explicit cache key version.
- Old metadata rows are not rewritten in place. They are expected to cold-miss
  naturally after the version bump.
- Search-result cache does not currently use a separate version namespace:
  raw provider search payloads stay keyed by provider/query/limit, while
  post-search ranking changes are isolated by the metadata cache version.
