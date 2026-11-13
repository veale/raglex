# Scraping service checks and recovery

RagLex's source scraping service is independent of the chat application's browser
connection. It uses `RAGLEX_SCRAPLING_MCP_URL` and the secret
`RAGLEX_SCRAPLING_MCP_KEY`; a working chat browser does not validate these values.

A useful health check has three parts:

1. Check the scraper container is running and its rendered-browser health probe
   passes. A JSON health endpoint alone does not prove browser navigation works.
2. Initialize MCP with the configured credential. HTTP 401 means a stale or
   incorrect credential, not an anti-bot response from the target publisher.
3. Fetch a known publisher page and check substantive content. An HTTP 200 error
   page, bot challenge, or EUR-Lex `errorDocumentView` is not law.

On 13 November 2026 the Asahi backend could not start because its requested
address on `cosmos-freshrss-default` had been assigned to `rssbox`. Reconnecting
**only the scraper's network attachment** with Docker-managed allocation let it
start. Its health and MCP session operations passed, and browser navigation of
the Ministry of Justice CPR Part 27 returned the expected final-remedy text.
RagLex's configured backend credential also differed from the scraper's current
credential; it was synchronized in the protected deployment environment and
settings store. Credential values are never part of this repository.

When diagnosing a similar startup error, inspect the affected network's address
assignments before restarting unrelated containers or the Docker daemon. Preserve
the scraper's other network memberships and the authentication gateway's route.
Docker persists the corrected attachment across container restarts; a separate
service manager that reconstructs containers must preserve that allocation too.

RagLex now stops tool-name probing on transport/authentication failures and real
browser failures. It falls back locally and applies a short shared cooldown
before retrying an unavailable backend. A newly configured credential has a
separate retry state. Restarted MCP sessions are discarded before retrying.
This prevents thousands of redundant attempts against one broken endpoint while
retaining recovery when it returns.

Source-specific failures still need source-specific treatment. For example,
HUDOC can return an empty HTTP 204 from a conversion URL, and EUR-Lex can return
an error or bot challenge with HTTP 200. These must remain explicit missing-body
conditions; neither is repaired merely by a healthy browser service.

## Retrying an outage worklist

`POST /api/jobs/harvest-all` accepts `references` (explicit stable IDs/candidates),
`limit`, `retry_cooled: true`, and `queue: true`. Build the list from recorded
transient failures rather than clearing every publisher-not-found cooldown. The job
reports each attempt's outcome and retains remaining publisher failures. A bounded
citation rescan similarly resolves only its reviewed document set.
