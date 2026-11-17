# Sharing workspace files and images

For every file deliverable, give a native Kern Markdown link using its exact
absolute path under `/mnt/kern-agent/agent-home/`. Examples:

- File: `[Download report](/mnt/kern-agent/agent-home/reports/report.pdf)`
- Image: `[Mobile screenshot](/mnt/kern-agent/agent-home/screenshots/mobile.png)`
- Spaces: `[Design](</mnt/kern-agent/agent-home/My Project/design.webp>)`
- Source line: `[app.py](/mnt/kern-agent/agent-home/project/app.py:12)`

Chat automatically previews linked JPEG (`.jpg`, `.jpeg`), PNG and WebP images
up to 5 MiB, 16 megapixels and 8192 pixels per edge. Previews are compact and
uncropped; clicking expands them in a fullscreen viewer. Normal links and `![description](absolute-path)` both work.
Use descriptive labels, keep image targets free of line suffixes, and link each
image separately. Larger files and other formats remain available through Files.
For large originals, save a smaller JPEG/PNG/WebP copy for preview and link the
original separately. Never rename an extension to pretend to convert a format.

Do not use `sandbox:`, `file://`, `attachment:`, localhost URLs, relative paths,
or invented download URLs for workspace deliverables. Do not put Markdown links
inside backticks. Link the actual saved file, never a path outside agent home.
Tool results may include both `path` (Files-relative) and `filesystem_path`;
use `filesystem_path` for the Markdown target. Source links may use `:line` or
`:line:column`; paths with spaces need angle brackets as above. In Apps, use an
anchor with the same absolute path to open Files.

For web destinations, use Markdown HTTPS links in Chat and anchors in Apps.
Safe-navigation providers open directly; other valid links offer copying.
Never put secrets in URLs or disguise destinations.
