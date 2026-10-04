"""Approved X posts and replies: validation, attempt limit, composer and submit."""
from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any, TYPE_CHECKING
from urllib.parse import urlsplit
from host.runtime.browser.client import BrowserError
from host.runtime.browser.providers import x
from host.runtime.core import host_errors

if TYPE_CHECKING:
    from host.runtime.browser.accounts import Profile

X_POST_DAILY_LIMIT = 50
TWEET_ID = re.compile(r"[0-9]{1,25}")


class PostRejected(BrowserError):
    """X returned an explicit rejection for this submission."""


class PreparationFailed(BrowserError):
    """A safe preparation step and cause type, without Playwright page content."""
    def __init__(self, step: str, cause: Exception) -> None:
        self.step = step
        self.failure_type = type(cause).__name__
        detail = str(cause) if isinstance(cause, BrowserError) else self.failure_type
        super().__init__(f"Preparation failed at {step}: {detail.rstrip('.')}.")


def validate_post_request(body: dict[str, Any]) -> tuple[str, str]:
    if set(body) not in ({"text"}, {"text", "in_reply_to_tweet_id"}):
        raise BrowserError("Expected text and an optional reply target ID.")
    text = body.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > 280 or any(ord(c) < 32 and c not in "\n\t" for c in text):
        raise BrowserError("Provide nonempty post text of at most 280 characters. X's composer also enforces its weighted limit.")
    reply_id = body.get("in_reply_to_tweet_id", "")
    if not isinstance(reply_id, str) or ("in_reply_to_tweet_id" in body and not TWEET_ID.fullmatch(reply_id)):
        raise BrowserError("in_reply_to_tweet_id must be a numeric X post ID.")
    return text, reply_id


def validate_post(body: dict[str, Any]) -> tuple[str, str, str]:
    account = x.validate_identifier(body.get("provider_identifier"))
    text, reply_id = validate_post_request({key: value for key, value in body.items() if key != "provider_identifier"})
    return account, text, reply_id


def _report_failure(stage: str, exc: Exception, *, step: str = "") -> None:
    # BrowserError messages are ours; Playwright/provider messages may contain
    # page content or credentials. Keep their type and stack, never their text.
    summary = str(exc) if isinstance(exc, BrowserError) else type(exc).__name__
    safe = RuntimeError(summary).with_traceback(exc.__traceback__)
    context = {"stage": stage, "failure_type": type(exc).__name__}
    if isinstance(exc, PreparationFailed):
        context.update(step=exc.step, failure_type=exc.failure_type)
    elif step:
        context["step"] = step
    host_errors.report_warning("browser.x_post_tweet", safe, context=context)


def execute(profile: Profile, body: dict[str, Any]) -> dict[str, Any]:
    if profile.provider.name != "x":
        raise BrowserError("x_post_tweet requires an X connection.")
    account, text, reply_id = validate_post(body)
    if profile.lease or profile.data["state"] != "connected" or account != profile.data["provider_identifier"]:
        raise BrowserError("Account login needs attention or is under operator control. Check the account in Browser settings.")
    day = datetime.now(timezone.utc).date().isoformat()
    usage = profile.data["usage"].get("x_post_tweet")
    count = usage["count"] if usage and usage["day"] == day else 0
    if count >= X_POST_DAILY_LIMIT:
        raise BrowserError("The daily browser posting limit has been reached.")
    try:
        step = "launch_browser"
        try:
            # Install the resource filter before X loads; prepare_post navigates
            # straight to the composer/target without first loading the home feed.
            browser = profile.launch(site="about:blank", block_media=True)
            step = "prepare_composer"
            prepare_post(browser.page, account, text, reply_id)
        except Exception as exc:
            _report_failure("prepare", exc, step=step)
            detail = str(exc) if isinstance(exc, BrowserError) else f"Preparation failed at {step} ({type(exc).__name__})."
            raise BrowserError(f"Post was not submitted. {detail} Open the browser to check the account; see Host diagnostics for the failed step.") from None
        profile.data["usage"]["x_post_tweet"] = {"day": day, "count": count + 1}
        profile.save()  # Count the attempt before the only submit click.
        try:
            url = submit_prepared_post(browser.page, account, reply_id)
        except PostRejected as exc:
            _report_failure("rejected", exc)
            raise
        except Exception as exc:
            _report_failure("confirm", exc)
            raise BrowserError("Could not confirm publication. The post may have been published. Check X before approving another attempt.") from None
        return {"status": "posted", "url": url}
    finally:
        try:
            profile.close()
        except Exception as exc:
            # Saving refreshed login state cannot change the submission result.
            _report_failure("cleanup", exc)


def prepare_post(page: Any, account: str, text: str, reply_id: str = "") -> None:
    from playwright.sync_api import expect  # type: ignore[import-not-found]
    step = "navigate_to_target" if reply_id else "navigate_to_composer"
    try:
        response = page.goto(f"https://x.com/i/status/{reply_id}" if reply_id else "https://x.com/compose/post", wait_until="domcontentloaded")
        if response is not None and response.status >= 400:
            raise BrowserError(f"X returned HTTP {response.status} while opening the post page")
        step = "verify_account"
        if x.verify_account(page) != account:
            raise BrowserError("The signed-in account changed. Reconnect in Browser settings.")
        if reply_id:
            step = "find_reply_target"
            target = page.locator('article[data-testid="tweet"]').filter(
                has=page.locator(f'a[href$="/status/{reply_id}"]'))
            expect(target).to_have_count(1, timeout=10000)
            step = "open_reply_composer"
            target.get_by_test_id("reply").click()
        step = "clear_composer"
        composer = page.get_by_role("dialog")
        editor = composer.get_by_test_id("tweetTextarea_0")
        editor.fill("")
        step = "type_post_text"
        # Keep normal keyboard/input handlers without spending 14 seconds of
        # the deadline on artificial delays for a 280-character post.
        editor.press_sequentially(text, delay=0, timeout=60000)
        step = "wait_for_submit_enabled"
        expect(composer.get_by_test_id("tweetButton")).to_be_enabled(timeout=10000)
        step = "verify_post_text"
        # Compare the property exactly, without text assertion whitespace
        # normalization, after the editor's input handlers have run.
        expect(editor).to_have_js_property("innerText", text, timeout=10000)
    except Exception as exc:
        raise PreparationFailed(step, exc).with_traceback(exc.__traceback__) from None


def submit_prepared_post(page: Any, account: str, reply_id: str = "") -> str:
    # Observe only the response for this click. No timeline scraping or
    # raw provider body is returned/logged. Failure after click must not be reported as success.
    with page.expect_response(
        lambda response: response.request.method == "POST"
        and urlsplit(response.url).hostname in {"x.com", "api.x.com"}
        and urlsplit(response.url).path.endswith("/CreateTweet"), timeout=20000,
    ) as pending:
        page.get_by_role("dialog").get_by_test_id("tweetButton").click(timeout=5000)
    response = pending.value
    if response.status != 200:
        raise BrowserError(f"X returned HTTP {response.status} instead of confirming the submission.")
    # Bounded expected shape; do not recursively search for unrelated IDs.
    payload = response.json()
    result = ((payload.get("data") or {}).get("create_tweet") or {}).get("tweet_results", {}).get("result")
    if not result and payload.get("errors"):
        code = payload["errors"][0].get("code")
        reason = {187: "duplicate post", 226: "automated activity restriction", 88: "rate limit"}.get(code) if type(code) is int else None
        detail = f" (code {code}" + (f", {reason}" if reason else "") + ")" if type(code) is int and 0 <= code <= 99999 else ""
        raise PostRejected(f"X rejected the submission{detail}. Open X to review the account before another attempt.")
    if result.get("__typename") == "TweetWithVisibilityResults":
        result = result["tweet"]
    tweet_id = result.get("rest_id", "")
    author = result.get("core", {}).get("user_results", {}).get("result", {})
    handle = author.get("core", {}).get("screen_name") or author.get("legacy", {}).get("screen_name", "")
    if not isinstance(tweet_id, str) or not re.fullmatch(r"[0-9]{1,25}", tweet_id) or str(handle).lower() != account:
        raise BrowserError("X did not confirm the submitted account and post.")
    if reply_id and result.get("legacy", {}).get("in_reply_to_status_id_str") != reply_id:
        raise BrowserError("X did not confirm the reply target.")
    return f"https://x.com/{account}/status/{tweet_id}"
