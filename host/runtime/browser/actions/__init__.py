"""Explicit service action routes; each action owns its provider checks and policy."""
from host.runtime.browser.actions.x_post_tweet import execute as post_tweet

from host.runtime.browser.actions.linkedin_dm import resolve, read, send

ACTIONS = {"post_tweet": post_tweet, "linkedin_resolve_recipient": resolve, "linkedin_read_conversation": read, "linkedin_send_dm": send}
