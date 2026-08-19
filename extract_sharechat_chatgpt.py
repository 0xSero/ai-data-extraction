import sys
import re
import json
import logging
import argparse
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any, List
from datetime import datetime

import traces_export

# Increase recursion limit for deep React Flight object graphs
sys.setrecursionlimit(10000)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

# Optional dependency for bypassing Cloudflare TLS fingerprinting
try:
    from curl_cffi import requests as cffi_requests

    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False
    import requests


@dataclass
class ChatMessage:
    role: str
    content: str
    create_time: Optional[float]
    id: str


class ChatGPTShareExtractor:
    """
    Extracts conversation data from ChatGPT shared links.
    Supports both legacy embedded JSON and modern React Server Components (Flight) payloads.
    """

    def __init__(self, url: str):
        self.url = url
        self.session = requests.Session() if not HAS_CURL_CFFI else None

    @property
    def share_id(self) -> str:
        m = re.search(r"/share/([A-Za-z0-9\-_]+)", self.url)
        return m.group(1) if m else "unknown_share"
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://chatgpt.com/",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }

    def fetch_html(self) -> str:
        logging.info(f"Fetching {self.url}")
        try:
            if HAS_CURL_CFFI:
                response = cffi_requests.get(
                    self.url, impersonate="chrome120", timeout=20
                )
            else:
                response = self.session.get(self.url, headers=self.headers, timeout=20)
            response.raise_for_status()
            return response.text
        except Exception as e:
            logging.error(f"Failed to fetch URL: {e}")
            raise

    def _extract_json_braces(
        self, html: str, start_pattern: str
    ) -> Optional[Dict[str, Any]]:
        """Extracts a JSON object from HTML by matching braces starting from a regex match."""
        match = re.search(start_pattern, html)
        if not match:
            return None

        start_index = match.end() - 1
        brace_count = 0
        in_string = False
        escape = False

        for i in range(start_index, len(html)):
            char = html[i]
            if escape:
                escape = False
                continue
            if char == "\\":
                escape = True
                continue
            if char == '"':
                in_string = not in_string
                continue
            if not in_string:
                if char == "{":
                    brace_count += 1
                elif char == "}":
                    brace_count -= 1
                    if brace_count == 0:
                        json_str = html[start_index : i + 1]
                        try:
                            return json.loads(json_str)
                        except json.JSONDecodeError:
                            return None
        return None

    def _extract_flight_payload(self, html: str) -> Optional[List[Any]]:
        """Extracts React Flight flattened array payload from streamController.enqueue calls."""
        scripts = re.finditer(r"<script\b[^>]*>(.*?)</script>", html, re.DOTALL)
        decoder = json.JSONDecoder()

        for match in scripts:
            text = match.group(1)
            if "streamController.enqueue" not in text:
                continue

            start = 0
            while True:
                anchor = text.find("streamController.enqueue(", start)
                if anchor == -1:
                    break
                anchor += len("streamController.enqueue(")

                # Look for the start of JSON
                quote_pos = text.find('"', anchor)
                next_close = text.find(");", anchor)

                if quote_pos != -1 and (next_close == -1 or quote_pos < next_close):
                    try:
                        chunk, end_offset = decoder.raw_decode(text, quote_pos)
                        chunk = self._coerce_flight_chunk(chunk)
                        if isinstance(chunk, list) and len(chunk) > 1:
                            return chunk
                        start = end_offset
                    except json.JSONDecodeError:
                        start = anchor + 1
                else:
                    try:
                        chunk, end_offset = decoder.raw_decode(text, anchor)
                        chunk = self._coerce_flight_chunk(chunk)
                        if isinstance(chunk, list) and len(chunk) > 1:
                            return chunk
                        start = end_offset
                    except json.JSONDecodeError:
                        start = anchor + 1
        return None

    @staticmethod
    def _coerce_flight_chunk(chunk: Any) -> Any:
        """Flight payloads may be embedded as an escaped JSON string. Unwrap
        string layers until we reach a list."""
        seen = 0
        while isinstance(chunk, str) and seen < 10:
            s = chunk.strip()
            if not (s.startswith("[") or s.startswith("{")):
                break
            try:
                chunk = json.loads(s)
            except json.JSONDecodeError:
                break
            seen += 1
        return chunk

    def _decode_flight_loader(self, loader: List[Any]) -> Dict[str, Any]:
        """Reconstructs object graph from flattened React Flight array."""
        cache: Dict[int, Any] = {}

        def resolve(value: Any) -> Any:
            if type(value) is int:
                if value in cache:
                    return cache[value]
                if not (0 <= value < len(loader)):
                    return value
                cache[value] = None
                resolved_value = resolve(loader[value])
                cache[value] = resolved_value
                return resolved_value
            if isinstance(value, list):
                return [resolve(item) for item in value]
            if isinstance(value, dict):
                return {k: resolve(v) for k, v in value.items()}
            return value

        resolved: Dict[str, Any] = {}
        iterator = iter(loader[1:])
        for key in iterator:
            try:
                value = next(iterator)
            except StopIteration:
                break
            if isinstance(key, str) and key not in resolved:
                resolved[key] = resolve(value)
        return resolved

    def extract_data(self) -> Dict[str, Any]:
        """Main extraction logic."""
        html = self.fetch_html()

        # 1. Try legacy / direct serverResponse
        server_resp = self._extract_json_braces(html, r'"serverResponse"\s*:\s*\{')
        if server_resp:
            logging.info("Found serverResponse literal.")
            return server_resp

        # 2. Try __NEXT_DATA__
        next_match = re.search(
            r'<script\s+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL
        )
        if next_match:
            try:
                next_data = json.loads(next_match.group(1))
                logging.info("Found __NEXT_DATA__.")
                return next_data
            except json.JSONDecodeError:
                pass

        # 3. Try modern React Flight payload
        flight_loader = self._extract_flight_payload(html)
        if flight_loader:
            logging.info("Found React Flight payload.")
            decoded = self._decode_flight_loader(flight_loader)
            if isinstance(decoded, dict) and "serverResponse" in decoded:
                sr = decoded["serverResponse"]
                if isinstance(sr, dict):
                    conv = self._find_conversation(sr)
                    if conv is not None:
                        return conv
                    if isinstance(sr.get("data"), dict):
                        return sr["data"]
                    return sr
            conv = self._find_conversation(decoded)
            if conv is not None:
                return conv
            loader_data = decoded.get("loaderData", {})
            if isinstance(loader_data, dict):
                for route_val in loader_data.values():
                    if isinstance(route_val, dict) and "serverResponse" in route_val:
                        return route_val["serverResponse"]

        raise ValueError(
            "Could not find any known ChatGPT conversation payload structure in the HTML."
        )

    @staticmethod
    def _find_conversation(obj: Any) -> Optional[Dict[str, Any]]:
        """Locate the conversation object inside a decoded Flight payload.

        The modern share format nests the conversation under arbitrary numeric
        property keys; we identify it as the first dict that holds an ordered
        message list (linear_conversation / _77).
        """
        if isinstance(obj, dict):
            for key in ("linear_conversation", "_77"):
                val = obj.get(key)
                if isinstance(val, list) and val:
                    return obj
            for val in obj.values():
                found = ChatGPTShareExtractor._find_conversation(val)
                if found is not None:
                    return found
        elif isinstance(obj, list):
            for item in obj[:200]:
                found = ChatGPTShareExtractor._find_conversation(item)
                if found is not None:
                    return found
        return None

    _ROLES = {"system", "user", "assistant", "tool"}
    _CONTENT_TYPES = {
        "text",
        "code",
        "reasoning",
        "analysis",
        "execution_output",
        "render",
        "image",
        "file",
        "tether",
        "tether_quote",
        "step",
        "multimodal_text",
        "thought",
        "input_text",
        "output_text",
    }

    @classmethod
    def _find_role(cls, node: Any) -> str:
        found = []

        def walk(o: Any, depth: int = 0) -> None:
            if found or depth > 18:
                return
            if isinstance(o, dict):
                for v in o.values():
                    if isinstance(v, str) and v in cls._ROLES:
                        found.append(v)
                        return
                    walk(v, depth + 1)
            elif isinstance(o, list):
                for x in o[:50]:
                    walk(x, depth + 1)

        walk(node)
        return found[0] if found else "unknown"

    @classmethod
    def _find_content(cls, node: Any) -> Optional[Dict[str, Any]]:
        found = []

        def walk(o: Any, depth: int = 0) -> None:
            if found or depth > 18:
                return
            if isinstance(o, dict):
                for v in o.values():
                    if isinstance(v, str) and v in cls._CONTENT_TYPES:
                        found.append(o)
                        return
                    walk(v, depth + 1)
            elif isinstance(o, list):
                for x in o[:50]:
                    walk(x, depth + 1)

        walk(node)
        return found[0] if found else None

    @staticmethod
    def _extract_text(content: Any) -> str:
        """Pull message text out of a Flight content object.

        Text parts are stored either as a list of strings (_265) or, for code
        blocks, as a single string field (_264)."""
        if isinstance(content, str):
            return content.strip()
        if not isinstance(content, dict):
            return ""
        str_lists = []
        texts = []
        for v in content.values():
            if isinstance(v, list):
                strs = [x for x in v if isinstance(x, str)]
                if strs:
                    str_lists.append("".join(strs))
            elif isinstance(v, str) and v not in ChatGPTShareExtractor._CONTENT_TYPES:
                texts.append(v)
        if str_lists:
            return "".join(str_lists).strip()
        if texts:
            return max(texts, key=len).strip()
        return ""

    def _parse_flight_conversation(
        self, conv: Dict[str, Any]
    ) -> tuple[str, List[ChatMessage]]:
        """Parses the modern React Flight conversation object."""
        title = conv.get("title") or conv.get("_45") or "Untitled Conversation"
        sequence = conv.get("linear_conversation") or conv.get("_77") or []

        messages = []
        for ent in sequence:
            if not isinstance(ent, dict):
                continue
            node = ent.get("_184")
            if not isinstance(node, dict):
                node = ent
            role = self._find_role(node)
            if role == "system":
                continue
            content = self._find_content(node)
            text = self._extract_text(content) if content else ""
            if not text:
                continue
            messages.append(
                ChatMessage(
                    role=role,
                    content=text,
                    create_time=node.get("_46"),
                    id=str(node.get("_183", "")),
                )
            )
        return title, messages

    def parse_messages(self, data: Dict[str, Any]) -> tuple[str, List[ChatMessage]]:
        """Parses the extracted data into a list of ChatMessage objects."""
        inner_data = data
        if "serverResponse" in data and isinstance(data["serverResponse"], dict):
            inner_data = data["serverResponse"]
        if "data" in inner_data and isinstance(inner_data["data"], dict):
            inner_data = inner_data["data"]

        mapping = inner_data.get("mapping", {})
        sequence = inner_data.get("linear_conversation", [])
        title = inner_data.get("title", "Untitled Conversation")

        messages = []

        if not sequence and mapping:
            nodes = [n for n in mapping.values() if isinstance(n, dict)]
            nodes.sort(key=lambda x: x.get("message", {}).get("create_time", 0))
            sequence = [{"id": n.get("id")} for n in nodes]

        # Modern React Flight format: ordered list of full node objects.
        if not sequence and not mapping:
            return self._parse_flight_conversation(inner_data)

        for entry in sequence:
            node_id = entry.get("id") if isinstance(entry, dict) else None
            if not node_id or node_id not in mapping:
                continue

            node = mapping[node_id]
            if not isinstance(node, dict):
                continue

            msg = node.get("message")
            if not isinstance(msg, dict):
                continue

            author = msg.get("author", {})
            role = author.get("role", "unknown")

            if role == "system":
                continue

            content = msg.get("content", {})
            text_parts = []

            if isinstance(content, dict):
                parts = content.get("parts", [])
                for part in parts:
                    if isinstance(part, str):
                        text_parts.append(part)
                    elif isinstance(part, dict):
                        text_parts.append(f"[{part.get('content_type', 'attachment')}]")
            elif isinstance(content, str):
                text_parts.append(content)

            text = "".join(text_parts).strip()
            if not text:
                continue

            messages.append(
                ChatMessage(
                    role=role,
                    content=text,
                    create_time=msg.get("create_time"),
                    id=msg.get("id", ""),
                )
            )

        return title, messages

    def extract(self) -> Dict[str, Any]:
        raw_data = self.extract_data()
        title, messages = self.parse_messages(raw_data)
        return {
            "title": title,
            "url": self.url,
            "message_count": len(messages),
            "messages": [asdict(m) for m in messages],
        }


def main():
    parser = argparse.ArgumentParser(
        description="Extract ChatGPT shared conversations."
    )
    parser.add_argument(
        "url", help="The ChatGPT share URL (e.g., https://chatgpt.com/share/...)"
    )
    parser.add_argument("-o", "--output", help="Output JSON file path", default=None)
    args = parser.parse_args()

    try:
        extractor = ChatGPTShareExtractor(args.url)
        result = extractor.extract()

        # Build the shared HF-traces conversation record so it funnels through
        # the same per-session JSONL writer every other extractor uses.
        conversation = {
            "session_id": extractor.share_id,
            "source": "chatgpt_share",
            "url": result["url"],
            "title": result["title"],
            "message_count": result["message_count"],
            "messages": result["messages"],
        }

        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(conversation, f, indent=2, ensure_ascii=False)
            logging.info(
                f"Successfully saved {result['message_count']} messages to {args.output}"
            )
        else:
            n_files, n_lines = traces_export.write_session_files(
                [conversation], "chatgpt"
            )
            logging.info(
                f"Successfully saved {result['message_count']} messages "
                f"across {n_files} session file(s) ({n_lines} lines) "
                f"to extracted_data/chatgpt/sessions/"
            )

    except Exception as e:
        logging.error(f"Extraction failed: {e}")
        raise


if __name__ == "__main__":
    main()
