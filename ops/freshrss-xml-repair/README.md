# FreshRSS publisher XML repair

Installed on Asahi as `xExtension-XmlFeedRepair`, enabled for user `mv`.
The extension only handles the two exact Rest of World latest-feed URLs. The
publisher's `media:content` attributes contain unescaped ampersands. It escapes
those ampersands, preserves existing entities and CDATA, and requires a valid RSS
root with at least one item before handing the response to SimplePie. HTTP errors
and other malformed content remain errors. TLS verification stays enabled.

Keep this directory in sync with the installed extension. The prior user
configuration is backed up as `config.php.before-xml-repair-20261114` on Asahi.
Remove the extension's user configuration entry and directory to roll back.
