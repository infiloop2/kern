"""Explicit service action routes; each action owns its provider checks and policy."""
from host.runtime.browser.actions.x_post_tweet import execute as post_tweet

ACTIONS = {"post_tweet": post_tweet}
