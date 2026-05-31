"""Feishu message sender — text, post, and rich card."""

import json
import sys
from typing import Optional
import requests

import lark_oapi
from lark_oapi.api.im.v1.model import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)


class FeishuSender:
    """Send messages to Feishu via HTTP API."""

    def __init__(self, app_id: str, app_secret: str, domain: str = "feishu"):
        self._app_id = app_id
        self._app_secret = app_secret
        self._domain = (
            "https://open.larksuite.com" if domain == "lark"
            else "https://open.feishu.cn"
        )
        self.client = lark_oapi.Client.builder() \
            .app_id(app_id) \
            .app_secret(app_secret) \
            .domain(self._domain) \
            .build()

    def send_text(self, chat_id: str, text: str,
                  reply_to: str = None) -> Optional[str]:
        """Send a plain text message. Returns message_id or None."""
        content = json.dumps({"text": text}, ensure_ascii=False)
        body = CreateMessageRequestBody.builder() \
            .receive_id(chat_id) \
            .msg_type("text") \
            .content(content) \
            .build()
        req = CreateMessageRequest.builder() \
            .receive_id_type("chat_id") \
            .request_body(body) \
            .build()
        try:
            resp = self.client.im.v1.message.create(req)
            if resp.success():
                return resp.data.message_id
            print(f"[sender] Send failed: code={resp.code} msg={resp.msg}",
                  file=sys.stderr)
        except Exception as e:
            print(f"[sender] Send error: {e}", file=sys.stderr)
        return None

    def send_post(self, chat_id: str, markdown: str,
                  reply_to: str = None) -> Optional[str]:
        """Send a rich-text post message from markdown."""
        print(f"[sender] Sending post ({len(markdown)} chars) ...", file=sys.stderr)
        try:
            from lark_oapi.channel.outbound.markdown.to_post import \
                markdown_to_post_ast
            post = markdown_to_post_ast(markdown)
            print(f"[sender] Post converted OK", file=sys.stderr)
        except Exception as e:
            print(f"[sender] Post convert failed: {e}, using fallback", file=sys.stderr)
            post = {"zh_cn": {"title": "", "content": [[{"tag": "md", "text": markdown}]]}}

        content = json.dumps(post, ensure_ascii=False)  # post already has zh_cn wrapper
        body = CreateMessageRequestBody.builder() \
            .receive_id(chat_id) \
            .msg_type("post") \
            .content(content) \
            .build()
        req = CreateMessageRequest.builder() \
            .receive_id_type("chat_id") \
            .request_body(body) \
            .build()
        try:
            resp = self.client.im.v1.message.create(req)
            if resp.success():
                print(f"[sender] Post sent OK", file=sys.stderr)
                return resp.data.message_id
            print(f"[sender] Post send failed: code={resp.code} msg={resp.msg}",
                  file=sys.stderr)
            # Fallback: try plain text
            return self.send_text(chat_id, markdown)
        except Exception as e:
            print(f"[sender] Post error: {e}", file=sys.stderr)
            return self.send_text(chat_id, markdown)

    def send_card(self, chat_id: str, card_dict: dict,
                  reply_to: str = None) -> Optional[str]:
        """Send an interactive card (CardKit 2.0)."""
        content = json.dumps(card_dict, ensure_ascii=False)
        body = CreateMessageRequestBody.builder() \
            .receive_id(chat_id) \
            .msg_type("interactive") \
            .content(content) \
            .build()
        req = CreateMessageRequest.builder() \
            .receive_id_type("chat_id") \
            .request_body(body) \
            .build()
        try:
            resp = self.client.im.v1.message.create(req)
            if resp.success():
                return resp.data.message_id
            print(f"[sender] Card send failed: code={resp.code} msg={resp.msg}",
                  file=sys.stderr)
        except Exception as e:
            print(f"[sender] Card error: {e}", file=sys.stderr)
        return None

    def reply_text(self, message_id: str, text: str) -> Optional[str]:
        """Reply to a specific message."""
        content = json.dumps({"text": text}, ensure_ascii=False)
        body = ReplyMessageRequestBody.builder() \
            .msg_type("text") \
            .content(content) \
            .build()
        req = ReplyMessageRequest.builder() \
            .message_id(message_id) \
            .request_body(body) \
            .build()
        try:
            resp = self.client.im.v1.message.reply(req)
            if resp.success():
                return resp.data.message_id
            print(f"[sender] Reply failed: code={resp.code} msg={resp.msg}",
                  file=sys.stderr)
        except Exception as e:
            print(f"[sender] Reply error: {e}", file=sys.stderr)
        return None

    def build_rich_card(self, markdown: str) -> dict:
        """Build a mixed-content interactive card from markdown.

        Uses Feishu's NATIVE table component (``type``: ``table``) for tables,
        which renders correctly on mobile with horizontal scroll.
        Markdown headings, text, and dividers become card elements.

        - # heading      → blue card header
        - ## / ###       → markdown element (font-size hierarchy preserved)
        - | table |      → native table component (not markdown text!)
        - ---            → hr divider
        - Between sections → auto hr
        - End            → note footer
        """
        lines = markdown.split('\n')

        # --- Extract first # heading as card header ---
        title = None
        body_start = 0
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith('# ') and not stripped.startswith('## '):
                title = stripped[2:].strip()
                body_start = i + 1
                break

        body_lines = lines[body_start:]

        # --- Parse body into semantic blocks ---
        blocks = self._parse_blocks(body_lines)

        # --- Build card elements ---
        elements = []
        prev_was_content = False

        for block in blocks:
            if block["type"] == "hr":
                elements.append({"tag": "hr"})
                prev_was_content = False
                continue

            if block["type"] == "heading":
                if prev_was_content:
                    elements.append({"tag": "hr"})
                elements.append(self._heading_to_element(block))
                prev_was_content = True
                continue

            if block["type"] == "table":
                if prev_was_content:
                    elements.append({"tag": "hr"})
                table_el = self._table_to_element(block)
                if table_el:
                    elements.append(table_el)
                prev_was_content = True
                continue

            # Text block
            text = '\n'.join(block["lines"]).strip()
            if text:
                elements.append({"tag": "markdown", "content": text})
                prev_was_content = True

        # Footer
        elements.append({
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": "🤖 Generated by Claude"}],
        })

        card = {
            "schema": "2.0",
            "config": {"streaming_mode": False},
            "body": {"elements": elements},
        }
        if title:
            card["header"] = {
                "title": {"tag": "plain_text", "content": title[:100]},
                "template": "blue",
            }

        return card

    # ---- block parsers ----

    @staticmethod
    def _parse_blocks(lines: list) -> list:
        """Group lines into semantic blocks: text, table, heading, hr."""
        blocks = []
        current = {"type": "text", "lines": []}

        for line in lines:
            stripped = line.strip()

            # Horizontal rule
            if stripped in ('---', '***', '___'):
                if current["lines"]:
                    blocks.append(current)
                blocks.append({"type": "hr", "lines": []})
                current = {"type": "text", "lines": []}
                continue

            # Table row
            if stripped.startswith('|') and '|' in stripped[1:]:
                if current["type"] != "table":
                    if current["lines"]:
                        blocks.append(current)
                    current = {"type": "table", "lines": []}
                current["lines"].append(line)
                continue

            # Heading
            if stripped.startswith('## ') or stripped.startswith('### '):
                if current["lines"]:
                    blocks.append(current)
                blocks.append({"type": "heading", "lines": [stripped]})
                current = {"type": "text", "lines": []}
                continue

            current["lines"].append(line)

        if current["lines"]:
            blocks.append(current)

        return blocks

    @staticmethod
    def _heading_to_element(block: dict) -> dict:
        """Convert a heading block to a card markdown element.

        Preserves ## / ### syntax so Feishu renders proper font sizes.
        """
        return {"tag": "markdown", "content": block["lines"][0]}

    @staticmethod
    def _table_to_element(block: dict):
        """Convert a markdown table block to a card markdown element.

        Feishu card markdown supports basic tables with pagination
        (5 rows per page on mobile). This is the best available option
        since the native ``table`` component is exclusive to CardKit
        templates (not available via the message API).
        """
        text = '\n'.join(block["lines"]).strip()
        if not text:
            return None
        return {"tag": "markdown", "content": text}

    @staticmethod
    def should_use_rich_card(text: str) -> bool:
        """Detect whether markdown text warrants a rich card.

        Returns True for structured content (tables, multiple sections, hr),
        False for simple text better suited to post format.
        """
        lines = text.split('\n')
        has_table = False
        heading_count = 0
        has_hr = False
        total_lines = len(lines)

        for line in lines:
            s = line.strip()
            if s.startswith('|') and '|' in s[1:]:
                has_table = True
            if s.startswith('## '):
                heading_count += 1
            if s in ('---', '***', '___'):
                has_hr = True

        # Rich card is for TWO scenarios:
        # 1. Comparison tables — multiple items side-by-side
        # 2. Long structured content — needs heading hierarchy for readability
        #
        # NOT for: short replies, single headings, casual markdown formatting
        if has_table:
            return True
        # 2+ headings + hr → explicit structure, worth a card
        if heading_count >= 2 and has_hr:
            return True
        # 3+ headings AND genuinely long (25+ lines) → needs visual hierarchy
        if heading_count >= 3 and total_lines > 25:
            return True
        return False

    def download_media(self, message_id: str, file_key: str,
                       file_type: str, out_path: str) -> bool:
        """Download media file from a message. Returns True on success."""
        try:
            # Get tenant access token (direct HTTP — same approach as Hermes)
            token_url = f"{self._domain}/open-apis/auth/v3/tenant_access_token/internal"
            token_resp = requests.post(token_url, json={
                "app_id": self._app_id,
                "app_secret": self._app_secret,
            }, timeout=10)
            token_resp.raise_for_status()
            token = token_resp.json()["tenant_access_token"]

            # Download file
            url = f"{self._domain}/open-apis/im/v1/messages/{message_id}/resources/{file_key}"
            resp = requests.get(
                url,
                params={"type": file_type},
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
            if resp.status_code == 200:
                with open(out_path, "wb") as f:
                    f.write(resp.content)
                print(f"[sender] Downloaded: {out_path} ({len(resp.content)} bytes)",
                      file=sys.stderr)
                return True
            print(f"[sender] Download failed: HTTP {resp.status_code} {resp.text[:200]}",
                  file=sys.stderr)
        except Exception as e:
            print(f"[sender] Download error: {e}", file=sys.stderr)
        return False
