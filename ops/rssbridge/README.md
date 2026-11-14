# Rapporteur Tech feed through Scrapling

`RapporteurTechBridge.php` retrieves the publisher's native Tech RSS through the
existing Asahi Scrapling MCP `make_request` tool with Chrome HTTP impersonation.
It does not use a local browser, broaden the feed to other topics, fetch paid
article bodies, or disable TLS verification. The publisher RSS supplies titles,
links, dates and descriptions. Unicode and XML entities are preserved. Empty,
malformed and challenge responses raise errors rather than publishing an empty feed.

Install the bridge in RSS Bridge's persistent `/config/bridges` mount and restart
RSS Bridge so its entrypoint copies it into `/app/bridges`. Configure an untracked
`/config/raglex-scrapling.json` with `url` and `token` keys for the existing MCP
service. The file must be mode 600, owned by the web user; the parent directory
must permit that user to traverse it. Keep this file outside the web root.
`RAGLEX_SCRAPLING_CONFIG` can override the path. Never commit this configuration.

The internal feed URL is:

```
http://rss-bridge/?action=display&bridge=RapporteurTech&format=Atom
```

The bridge caches for 30 minutes and closes its MCP transport; `make_request`
automatically closes its own HTTP session. It accepts only the publisher's fixed
Tech feed URL, not arbitrary user-supplied URLs. Check real entry titles and
publisher links when verifying: RSS Bridge can represent an error as a single
Atom entry with HTTP 200. Remove only this bridge's cached error response when
retrying a repaired configuration.

Deployed on Asahi and verified from FreshRSS: 100 real entries imported. The
original feed URL is backed up in the FreshRSS user's private data directory.
Rollback by restoring that URL and removing the bridge/configuration.
