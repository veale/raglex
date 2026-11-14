<?php
final class XmlFeedRepairExtension extends Minz_Extension {
    public function init(): void {
        parent::init();
        $this->registerHook('simplepie_before_init', [$this, 'repairFeed']);
    }
    public static function normalise(string $xml): ?string {
        // The publisher emits bare '&' in media URLs. CDATA is already literal
        // and must stay untouched; existing XML/numeric entities stay intact too.
        $parts = preg_split('/(<!\[CDATA\[.*?\]\]>)/s', $xml, -1, PREG_SPLIT_DELIM_CAPTURE);
        if (!is_array($parts)) return null;
        foreach ($parts as &$part) {
            if (!str_starts_with($part, '<![CDATA[')) {
                $part = preg_replace('/&(?!amp;|lt;|gt;|quot;|apos;|#\d+;|#x[0-9a-f]+;)/i', '&amp;', $part) ?? $part;
            }
        }
        unset($part);
        $fixed = implode('', $parts);
        $previous = libxml_use_internal_errors(true);
        $doc = new DOMDocument();
        $valid = $doc->loadXML($fixed, LIBXML_NONET) && $doc->documentElement?->tagName === 'rss'
            && $doc->getElementsByTagName('item')->length > 0;
        libxml_clear_errors();libxml_use_internal_errors($previous);
        return $valid ? $fixed : null;
    }
    public function repairFeed(FreshRSS_SimplePieCustom $simplePie, FreshRSS_Feed $feed): void {
        $url = $feed->url();
        if (!in_array($url, ['https://restofworld.org/feed/latest/', 'https://restofworld.org/feed/latest'], true)) return;
        $curl = curl_init($url);
        curl_setopt_array($curl, [CURLOPT_RETURNTRANSFER=>true, CURLOPT_TIMEOUT=>25,
            CURLOPT_FOLLOWLOCATION=>false, CURLOPT_SSL_VERIFYPEER=>true, CURLOPT_SSL_VERIFYHOST=>2,
            CURLOPT_USERAGENT=>'FreshRSS Publisher XML Repair']);
        $body = curl_exec($curl);$status = curl_getinfo($curl, CURLINFO_HTTP_CODE);curl_close($curl);
        if ($status !== 200 || !is_string($body)) return;
        $fixed = self::normalise($body);
        if ($fixed !== null) {
            // SimplePie gives a configured URL precedence over set_raw_data().
            $simplePie->feed_url = null;
            $simplePie->permanent_url = $url;
            $simplePie->enable_cache(false);
            $simplePie->set_raw_data($fixed);
        }
    }
}
