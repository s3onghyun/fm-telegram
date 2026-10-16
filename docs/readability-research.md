# Reply readability research

Researched: 2026-10-15

## Implementations reviewed

- [sudoskys/telegramify-markdown](https://github.com/sudoskys/telegramify-markdown) — converts LLM-generated Markdown into Telegram entities and splits entities by UTF-16 offset. The strongest candidate if long Markdown, Mermaid or file attachments become requirements.
- [Tishka17/sulguk](https://github.com/Tishka17/sulguk) — a Python library that converts HTML into Telegram entities, avoiding the whitespace and restricted-tag issues of `parse_mode=HTML`. Confirms that the entity approach is safer for complex HTML.
- [Telegram Bot API formatting options](https://core.telegram.org/bots/api#formatting-options) — both HTML and MarkdownV2 restrict formatting and nesting. MarkdownV2 has different escaping rules for plain text, code and links, which makes it fragile for LLM output. `sendMessage` text must be 1–4096 characters.

Both projects are well maintained, but the syntax this bridge needs is small and it currently uses only the standard library.

## Choice: restricted Markdown → safe Telegram HTML

The bridge uses `parse_mode=HTML` but never trusts input HTML. It generates only:

- `**bold**`, `*italic*`, `` `inline code` ``, fenced code blocks, `[links](https://…)`
- lists flattened to `•`, keeping only the indentation on small screens
- Markdown tables rendered as `• header: value · header: value`
- every other `<`, `>`, `&` and any raw HTML is escaped

Telegram has no `<table>`, `<ul>` or `<li>` tags, so tables are not forced into alignment. Generated HTML is split below 4096 characters, preferring paragraph, line and word boundaries; at a cut, open tags are closed and reopened in the next part. If Telegram rejects the entities, the same part is resent once as plain text without tags.

## Why no dependency, and the limits

`telegramify-markdown` is a good, tested choice for UTF-16 entity offsets, wider Markdown and advanced splitting. However, it pulls in the Rust-based `pyromark` and would add a Markdown parser and a new runtime dependency to this small single-file bridge. `sulguk` likewise adds an entity-processing layer.

For the current scope, generating only allowed tags with the standard library is smaller and easier to audit. If complex nested Markdown, block quotes, spoilers, images, Mermaid or exact UTF-16 entity control become product requirements, switch to the `telegramify-markdown` entity + `split_entities()` approach instead of extending the regular expressions.
