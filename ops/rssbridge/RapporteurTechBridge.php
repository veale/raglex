<?php

/** Use the existing Asahi Scrapling HTTP client; no local browser dependency. */
class RapporteurTechBridge extends BridgeAbstract
{
    const NAME = 'Rapporteur Tech (formerly Euractiv)';
    const URI = 'https://www.rapporteur.com/sections/tech/';
    const DESCRIPTION = 'Publisher Tech RSS through the authenticated Asahi Scrapling service';
    const MAINTAINER = 'RagLex maintenance';
    const CACHE_TIMEOUT = 1800;

    public function collectData()
    {
        $path = getenv('RAGLEX_SCRAPLING_CONFIG') ?: '/config/raglex-scrapling.json';
        $config = json_decode(file_get_contents($path), true, 512, JSON_THROW_ON_ERROR);
        $session = null;
        try {
            self::rpc($config, ['jsonrpc'=>'2.0', 'id'=>0, 'method'=>'initialize', 'params'=>[
                'protocolVersion'=>'2025-06-18', 'capabilities'=>(object)[],
                'clientInfo'=>['name'=>'rssbridge-raglex', 'version'=>'1.0'],
            ]], $session);
            self::rpc($config, ['jsonrpc'=>'2.0', 'method'=>'notifications/initialized'], $session);
            $reply = self::rpc($config, ['jsonrpc'=>'2.0', 'id'=>1, 'method'=>'tools/call', 'params'=>[
                'name'=>'make_request', 'arguments'=>[
                    'url'=>'https://www.rapporteur.com/sections/tech/feed/',
                    'impersonate'=>'chrome', 'verify'=>true, 'timeout'=>30,
                    'extraction_type'=>'html', 'main_content_only'=>false,
                ],
            ]], $session);
            $result = $reply['result'] ?? [];
            if (!empty($result['isError'])) {
                throw new RuntimeException('Scrapling reported a feed retrieval error');
            }
            $data = $result['structuredContent'] ?? null;
            if (!$data) {
                foreach ($result['content'] ?? [] as $block) {
                    if (($block['type'] ?? '') === 'text') {
                        $data = json_decode($block['text'], true);
                        if ($data) { break; }
                    }
                }
            }
            if (($data['status'] ?? 0) !== 200) {
                throw new RuntimeException('Publisher feed did not return HTTP 200');
            }
            $this->items = self::parseFeed($data['content'][0] ?? '');
        } finally {
            // make_request closes its own HTTP session. Close the MCP transport too.
            if ($session) {
                $ch = curl_init(rtrim($config['url'], '/') . '/');
                curl_setopt_array($ch, [CURLOPT_CUSTOMREQUEST=>'DELETE', CURLOPT_RETURNTRANSFER=>true,
                    CURLOPT_TIMEOUT=>10, CURLOPT_HTTPHEADER=>[
                        'Authorization: Bearer '.$config['token'], 'Mcp-Session-Id: '.$session]]);
                curl_exec($ch);
                curl_close($ch);
            }
        }
    }

    private static function rpc(array $config, array $body, ?string &$session): array
    {
        $headers = ['Content-Type: application/json', 'Accept: application/json, text/event-stream',
                    'Authorization: Bearer '.$config['token']];
        if ($session) { $headers[] = 'Mcp-Session-Id: '.$session; }
        $ch = curl_init(rtrim($config['url'], '/') . '/');
        curl_setopt_array($ch, [CURLOPT_POST=>true, CURLOPT_POSTFIELDS=>json_encode($body),
            CURLOPT_RETURNTRANSFER=>true, CURLOPT_FOLLOWLOCATION=>true, CURLOPT_TIMEOUT=>45,
            CURLOPT_HTTPHEADER=>$headers,
            CURLOPT_HEADERFUNCTION=>static function ($curl, $line) use (&$session) {
                if (stripos($line, 'mcp-session-id:') === 0) { $session = trim(substr($line, 15)); }
                return strlen($line);
            }]);
        $raw = curl_exec($ch);
        $status = curl_getinfo($ch, CURLINFO_HTTP_CODE);
        curl_close($ch);
        if ($status < 200 || $status >= 300 || $raw === false) {
            throw new RuntimeException('Scrapling transport failed (HTTP '.$status.')');
        }
        if (!isset($body['id'])) { return []; }
        $reply = json_decode($raw, true);
        if (!$reply) {
            foreach (preg_split('/\r?\n/', $raw) as $line) {
                if (str_starts_with($line, 'data:')) {
                    $candidate = json_decode(trim(substr($line, 5)), true);
                    if (($candidate['id'] ?? null) === $body['id']) { $reply = $candidate; break; }
                }
            }
        }
        if (!$reply || isset($reply['error'])) { throw new RuntimeException('Invalid MCP reply'); }
        return $reply;
    }

    public static function parseFeed(string $html): array
    {
        $doc = new DOMDocument();
        if (!@$doc->loadXML($html, LIBXML_NONET) && !@$doc->loadHTML('<?xml encoding="UTF-8"?>'.$html, LIBXML_NONET)) {
            throw new RuntimeException('Invalid publisher feed markup');
        }
        $items = [];
        foreach ($doc->getElementsByTagName('item') as $entry) {
            $field = static function ($name) use ($entry) {
                foreach ($entry->childNodes as $node) {
                    if (strtolower($node->nodeName) === $name) { return trim($node->textContent); }
                }
                return '';
            };
            $uri = $field('link');
            // HTML serialisation may treat RSS <link> as a void HTML element.
            if (!$uri) {
                foreach ($entry->childNodes as $node) {
                    if (strtolower($node->nodeName) === 'link' && $node->nextSibling instanceof DOMText) {
                        $uri = trim($node->nextSibling->textContent); break;
                    }
                }
            }
            if (!str_starts_with($uri, 'https://www.rapporteur.com/')) { $uri = $field('guid'); }
            $title = $field('title');
            $stamp = strtotime($field('pubdate'));
            if (!$title || !str_starts_with($uri, 'https://www.rapporteur.com/') || !$stamp) {
                throw new RuntimeException('Publisher feed entry lacks verified title, link or date');
            }
            $items[] = ['title'=>$title, 'uri'=>$uri, 'uid'=>$field('guid') ?: $uri,
                        'timestamp'=>$stamp, 'content'=>$field('description')];
        }
        if (!$items) { throw new RuntimeException('Publisher returned no feed entries'); }
        return $items;
    }
}
