"""Exercise real sidebar handles against a durable index fixture."""

import re
from typing import Any


def run(page: Any, url: str, log_in: Any, *, touch: bool = False) -> None:
    from playwright.sync_api import expect

    print(f"Navigation order smoke: touch={touch}", flush=True)

    groups = {
        "apps": [{"app_id": f"app-{i}", "name": f"App {i}", "revision": 0} for i in range(1, 4)],
        "schedules": [{"thread_id": f"schedule-{i}", "name": f"Schedule {i}"} for i in range(1, 4)],
    }
    writes = []
    reject = False

    def route_index(route: Any) -> None:
        kind = "apps" if "/web-apps/" in route.request.url else "schedules"
        key = "app_id" if kind == "apps" else "thread_id"
        if route.request.method == "POST":
            if reject:
                route.fulfill(status=409, json={"error": {"message": "The list changed; try again"}})
                return
            body = route.request.post_data_json
            writes.append(body)
            rows = groups[kind]
            item = next(row for row in rows if row[key] == body["item_id"])
            rows.remove(item)
            before = body["before_id"]
            index = len(rows) if before is None else next(i for i, row in enumerate(rows) if row[key] == before)
            rows.insert(index, item)
            route.fulfill(json={"status": "ok"})
        else:
            route.fulfill(json={"apps" if kind == "apps" else "threads": groups[kind]})

    patterns = [
        re.compile(r"/v1/workspace/web-apps/apps(?:/order)?$"),
        re.compile(r"/v1/workspace/chat/scheduled-agents(?:/order)?$"),
    ]
    for pattern in patterns:
        page.context.route(pattern, route_index)
    log_in(page, url)

    def open_nav(target: Any) -> None:
        expect(target.locator("#app")).to_be_visible()
        button = target.get_by_role("button", name="Open navigation", exact=True)
        if button.is_visible():
            button.click()

    open_nav(page)
    original_url = page.url
    apps = page.locator("#web-apps-nav-items")
    schedules = page.locator("#scheduled-agents-nav-items")
    expect(apps.locator(".workspace-nav-label")).to_have_text(["App 1", "App 2", "App 3"])
    expect(page.locator("#chat-nav-items .workspace-reorder-handle")).to_have_count(0)
    page.locator('[data-reorder-id="app-2"]').focus()
    page.keyboard.press("ArrowUp")
    expect(apps.locator(".workspace-nav-label")).to_have_text(["App 2", "App 1", "App 3"])
    expect(page.locator('[data-reorder-id="app-2"]')).to_be_focused()
    page.keyboard.press("ArrowUp")  # Already first: no write.
    assert len(writes) == 1

    def drag_handle(source: Any, target: Any, *, cancel: bool = False) -> None:
        # A refresh can detach a handle before Locator.evaluate runs. Resolve,
        # center the group away from edge scrolling, and measure in one task.
        bounds = page.wait_for_function("""([sourceId, targetId]) => {
            const handles = [sourceId, targetId].map(id =>
                document.querySelector(`[data-reorder-id="${CSS.escape(id)}"]`));
            if (handles.some(el => !el)) return false;
            handles[0].parentElement.parentElement.scrollIntoView({
                block: 'center', inline: 'nearest', behavior: 'instant',
            });
            const rects = handles.map(el => el.getBoundingClientRect());
            return rects.every(rect => rect && rect.width > 0 && rect.height > 0)
                ? rects.map(rect => ({x: rect.x, y: rect.y, width: rect.width, height: rect.height}))
                : false;
        }""", arg=[source.get_attribute("data-reorder-id"), target.get_attribute("data-reorder-id")])
        a, b = bounds.json_value()
        bounds.dispose()
        x, y = a["x"] + a["width"] / 2, a["y"] + a["height"] / 2
        end_y = b["y"] + 2
        if touch:
            session = page.context.new_cdp_session(page)
            session.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
            session.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x, "y": end_y}]})
        else:
            page.mouse.move(x, y)
            page.mouse.down()
            page.mouse.move(x, end_y, steps=5)
        # A periodic refresh must not replace a captured pointer's element.
        page.evaluate("() => window.KernHost.refreshNavigation()")
        expect(source).to_be_attached()
        # Pointer input and the refresh can scroll the sidebar. Aim at the
        # target's current position, as an operator following the drop line does.
        end_y = target.bounding_box()["y"] + 2
        if touch:
            session.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x, "y": end_y}]})
        else:
            page.mouse.move(x, end_y)
        expect(target.locator("..")).to_have_class(re.compile(r"workspace-reorder-before"))
        if cancel:
            page.keyboard.press("Escape")
        if touch:
            session.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
            session.detach()
        else:
            page.mouse.up()

    source = page.locator('[data-reorder-id="schedule-3"]')
    target = page.locator('[data-reorder-id="schedule-1"]')
    drag_handle(source, target)
    expect(schedules.locator(".workspace-nav-label")).to_have_text(["Schedule 3", "Schedule 1", "Schedule 2"])
    assert page.url == original_url  # Dragging did not open a thread.
    drag_handle(page.locator('[data-reorder-id="schedule-2"]'), source, cancel=True)
    expect(schedules.locator(".workspace-nav-label")).to_have_text(["Schedule 3", "Schedule 1", "Schedule 2"])
    assert len(writes) == 2

    groups["apps"][1]["last_used_at"] = "2099-01-01T00:00:00Z"
    groups["apps"][1]["revision"] = 10
    page.evaluate("() => window.KernHost.refreshNavigation()")
    expect(apps.locator(".workspace-nav-label")).to_have_text(["App 2", "App 1", "App 3"])
    reject = True
    page.locator('[data-reorder-id="app-3"]').focus()
    page.keyboard.press("ArrowUp")
    expect(page.locator("#notice")).to_contain_text("The list changed")
    expect(apps.locator(".workspace-nav-label")).to_have_text(["App 2", "App 1", "App 3"])
    reject = False
    page.reload()
    open_nav(page)
    expect(apps.locator(".workspace-nav-label")).to_have_text(["App 2", "App 1", "App 3"])
    expect(schedules.locator(".workspace-nav-label")).to_have_text(["Schedule 3", "Schedule 1", "Schedule 2"])
    # A second page has no browser-local order to inherit.
    second = page.context.new_page()
    second.goto(url)
    open_nav(second)
    expect(second.locator("#web-apps-nav-items .workspace-nav-label")).to_have_text(["App 2", "App 1", "App 3"])
    second.close()
