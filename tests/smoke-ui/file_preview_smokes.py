"""Download-only previews must leave the viewer empty and navigation responsive."""

from urllib.parse import parse_qs, urlsplit


def run(page, url, log_in):
    from playwright.sync_api import expect

    cases = {
        "/workspace/kit.zip": "unsupported_type",
        "/workspace/unknown.custom": "unsupported_type",
        "/workspace/binary.txt": "binary",
        "/workspace/large.txt": "too_large",
        "/workspace/large.mp4": "too_large",
    }
    downloads = []
    previews = []
    payload = b"PK\x03\x04original download bytes\x00\xff"

    def file_response(route):
        parsed = urlsplit(route.request.url)
        path = parse_qs(parsed.query).get("path", [""])[0]
        if path not in cases:
            route.continue_()
        elif parsed.path.endswith("/download"):
            downloads.append(path)
            route.fulfill(body=payload, content_type="application/octet-stream")
        elif parsed.path.endswith("/content"):
            previews.append(path)
            route.fulfill(status=400, json={"error": {"message": "file is larger than 26214400 bytes"}})
        else:
            previews.append(path)
            route.fulfill(json={
                "path": path, "size_bytes": 10033207,
                "preview_unavailable": cases[path],
            })

    page.route("**/v1/agent-files/read?*", file_response)
    page.route("**/v1/agent-files/content?*", file_response)
    page.route("**/v1/agent-files/download?*", file_response)
    log_in(page, url)
    for path, reason in cases.items():
        # Start with an image so every fallback must clear the previous media.
        page.evaluate("() => window.KernHost.openAgentFile('/workspace/screenshot.png')")
        expect(page.locator("#file-image")).to_be_visible()
        # This is also the entry point for file links outside the Files tab.
        page.evaluate("path => window.KernHost.openAgentFile(path)", path)
        assert path in previews, f"Preview request did not reach its fixture: {path}"
        expect(page.locator("#file-viewer-title")).to_have_text(path)
        expect(page.locator("#file-content")).to_be_hidden()
        expect(page.locator("#file-content")).to_have_text("")
        for media in ["#file-image", "#file-video"]:
            expect(page.locator(media)).to_be_hidden()
            assert page.locator(media).get_attribute("src") is None
        expect(page.locator("#file-preview-message")).to_contain_text(
            "too large to preview" if reason == "too_large" else "Preview is unavailable"
        )
        expect(page.locator("#file-download")).to_be_visible()
        # Directory refreshes must not erase the viewer's explanation.
        page.locator("#file-path").press("Enter")
        expect(page.locator("#file-preview-message")).to_be_visible()
        assert path not in downloads, "Opening a file must not trigger a download"
        with page.expect_download() as download_info:
            page.locator("#file-download").click()
        download = download_info.value
        assert download.suggested_filename == path.rsplit("/", 1)[1]
        assert download.path().read_bytes() == payload
        assert downloads[-1] == path
        # Exercise real navigation immediately after the unsupported preview.
        page.locator("#file-list").get_by_role("button", name="notes.txt", exact=True).click()
        expect(page.locator("#file-content")).to_be_visible()
        expect(page.locator("#file-content")).to_contain_text("Mobile audit fixes")
        expect(page.locator("#file-message")).to_have_text("")
        expect(page.locator("#file-preview-message")).to_be_hidden()
    print("PASS: download-only file previews, unchanged download bytes, and navigation")
