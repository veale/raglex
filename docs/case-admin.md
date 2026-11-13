# Admin case removal and refresh

In **Admin → Maintain → Remove or refresh a case**, enter a case citation or
stable ID and choose **Find case**. Review the matched title and identity.

**Force refetch** starts a background job that fetches the publisher's case even
when RagLex already holds it. It replaces the extracted text and segments even
if the source bytes are unchanged, then refreshes citations and the text index.
A failed or empty fetch retains the held copy. Sources without an individual-case
fetch route show that limitation rather than pretending to refresh.

**Purge case… → Confirm purge** removes the matched case from the catalogue,
including its versions, attachments' catalogue entries, aliases, outgoing
citations, tags, and search/vector index entries. Citations in other documents
remain as unresolved references: those references belong to the citing source.
Purge is atomic and runs through the job system. It creates **no permanent
suppression or re-import block**; a later publisher republication can be
harvested normally.

Content-addressed filesystem objects are not deleted blindly because other
records or versions may share them. Removing their catalogue entries makes the
purged case and its originals unavailable through RagLex's reader/API.

Both preview and actions require the admin role. The API also requires the
reviewed `expected_id`, so an alias that changes between preview and confirmation
cannot silently purge another case.

## API

- `GET /api/case-admin/preview?citation=...`
- `POST /api/case-admin/action` with `citation`, `expected_id`, and
  `action` (`purge` or `refetch`). The response contains a durable `job_id`.
- `GET /api/jobs/{job_id}` reports progress and the result.

Ordinary source disappearance remains an upstream-status observation. A purge
is an explicit admin operation, not an automatic reaction to a transient 404.
