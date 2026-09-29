"""An integration helper must wait for rendered inputs, not response headers."""


def run(page, url, log_in, open_home_integration):
    from playwright.sync_api import expect

    log_in(page, url)
    open_home_integration(page, "tool:brave_search")
    page.locator("#panel-network .home-back").click()
    previous_input = page.query_selector("#tool-config-brave_search-BRAVE_SEARCH_API_KEY")
    assert previous_input is not None
    # Inject slow JSON consumption after the actual response arrives. This
    # exposes the header-vs-render race without sleeping in the test or
    # extending assertion timeouts. Home's unrelated /v1/tools request may
    # also finish while the integration is opening.
    page.evaluate("""() => {
        const originalFetch = window.fetch;
        window.fetch = async (...args) => {
            const response = await originalFetch(...args);
            if (String(args[0]) === '/v1/tools' && location.hash.includes('integrations')) {
                const json = response.json.bind(response);
                response.json = async () => {
                    const body = await json();
                    await new Promise(resolve => setTimeout(resolve, 1000));
                    return body;
                };
            }
            return response;
        };
    }""")
    try:
        open_home_integration(page, "tool:brave_search")
        assert not previous_input.evaluate("node => node.isConnected"), (
            "Integration helper returned before the refreshed tool inputs rendered"
        )
        field = page.locator("#tool-config-brave_search-BRAVE_SEARCH_API_KEY")
        row = page.locator("#tools [data-tool-row='brave_search']")
        status = row.locator(".config-key .status")
        save = row.locator("[data-action='save-tool-config'][data-key='BRAVE_SEARCH_API_KEY']")
        field.fill("mock-brave-key")
        with page.expect_request(lambda request: request.method == "PUT" and request.url.endswith("/v1/tools/brave_search/config")) as write:
            save.click()
        assert write.value.post_data_json == {"key": "BRAVE_SEARCH_API_KEY", "value": "mock-brave-key"}
        expect(status).to_have_text("set")
        expect(field).to_have_value("")
        # Restore the shared mock's original unconfigured state for the
        # remaining core journeys.
        save.click()
        expect(status).to_have_text("not set")
        print("Integration config: delayed JSON waits for fresh inputs and saves the entered key", flush=True)
    finally:
        previous_input.dispose()
