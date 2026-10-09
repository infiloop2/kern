"""Read the composer's logical text without layout-generated line breaks."""
from typing import Any


def active_composer(page: Any) -> Any:
    # Ignore unrelated dialogs and the inline home-feed editor. Never choose
    # the first match: multiple visible composer dialogs must fail closed.
    return page.get_by_role("dialog").filter(
        has=page.locator('[data-testid="tweetTextarea_0"]:visible')).filter(visible=True)


# X's Draft editor uses block elements, placeholder BRs for empty blocks,
# inline decorations for links and IMG alt text for emoji. innerText adds an
# extra newline for empty blocks and omits image emoji; textContent loses all
# block boundaries. Read those structures without trimming/collapsing text.
# Run in the page so polling never sends unapproved page content to diagnostics.
MATCHES_TEXT = r"""([editor, expected]) => {
    if (!editor.isConnected) return false;
    const isBlock = node => node === editor || node.nodeName === 'DIV' || node.nodeName === 'P';
    function placeholder(node) {
        while (node.childNodes.length === 1 && node.firstChild.nodeType === Node.ELEMENT_NODE) {
            node = node.firstChild;
            if (node.nodeName === 'BR') return true;
        }
        return false;
    }
    function read(node) {
        if (node.nodeType === Node.TEXT_NODE) return node.nodeValue;
        if (node.nodeType !== Node.ELEMENT_NODE) return '';
        if (node.tagName === 'IMG') return node.getAttribute('alt') || '\uFFFC';
        if (node.tagName === 'BR') return '\n';
        const children = Array.from(node.childNodes);
        // A sole BR keeps an empty editable block visible; it is not content.
        if (isBlock(node) && placeholder(node)) return '';
        let text = '';
        let previousBlock = false;
        children.forEach((child, index) => {
            const block = isBlock(child);
            if (index && (block || previousBlock)) text += '\n';
            text += read(child);
            previousBlock = block;
        });
        return text;
    }
    return read(editor) === expected;
}"""
