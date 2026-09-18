"""Small, deterministic task queries; no model call or persistent recall state."""
from __future__ import annotations

import re

from host import memory_recall_rules as rules
from host.agent_scripts import AUTOMATED_TRIGGER_PREFIX, LEGACY_AUTOMATED_TRIGGER_PREFIX



def strip_automated_prefix(message: str) -> str:
    for prefix in (AUTOMATED_TRIGGER_PREFIX, LEGACY_AUTOMATED_TRIGGER_PREFIX):
        message = message.removeprefix(prefix)
    return message


def topic_terms(message: str) -> list[str]:
    message = strip_automated_prefix(message)
    if re.fullmatch(rules.BARE_CONTINUATION_PATTERN, message, re.I):
        return []
    words = re.findall(rules.TOKEN_PATTERN, message)
    terms: list[str] = []
    for index, word in enumerate(words):
        if re.search(rules.NEGATIVE_CONTRACTION_PATTERN, word, re.I):
            continue
        word = re.sub(rules.WORD_SUFFIX_PATTERN, "", word, flags=re.I)
        token = word.lower()
        is_acronym = len(word) > 1 and word.isupper()
        if message.isupper() and len(words) > 1 and token in rules.STOPWORDS:
            # An all-caps sentence still has grammatical words. Preserve the
            # common ambiguous topic abbreviations rather than grammatical YOU/etc.
            is_acronym = word in rules.ALL_CAPS_TOPICS
        # These ambiguous modal words also name a month/person. Sentence
        # openers such as Can/How/What remain ordinary function words.
        named_stopword = word in rules.NAMED_STOPWORDS
        if (index == 0 and token in rules.MODAL_OPENERS and len(words) > 1
                and words[1].lower() in rules.SUBJECT_PRONOUNS):
            is_acronym = named_stopword = False
        if token in rules.QUERY_FILLER or (token in rules.STOPWORDS and not (is_acronym or named_stopword)):
            continue
        # Preserve acronym spelling so normalizing again in Workspace cannot
        # turn IT/US or names such as May into stopwords. Keep C++/C# distinct.
        # Lowercase expansion (e.g. İ -> i + combining dot) must not create
        # extra tokens when Workspace normalizes the admission query again.
        terms.append(word if is_acronym or named_stopword or len(token) != len(word) else token)
    return list(dict.fromkeys(terms))


def task_query(message: str) -> str:
    terms: list[str] = []
    size = 0
    topics = topic_terms(message)
    # In wholly uppercase sentences OR can still be a conjunction. Preserve
    # websearch syntax there; mixed-case or standalone OR keeps acronym intent.
    quote_operators = not message.isupper() or len(topics) == 1
    for term in topics:
        term = f'"{term}"' if term in rules.AMBIGUOUS_SEARCH_OPERATORS and quote_operators else term
        added = len(term.encode("utf-8")) + bool(terms)
        if size + added > rules.MAX_QUERY_BYTES:
            break
        terms.append(term)
        size += added
    return " ".join(terms)


def query_continues(message: str) -> bool:
    message = strip_automated_prefix(message)
    reference_message = message.lower() if re.match(rules.IT_COMMAND_PATTERN, message, re.I) else message
    remainder = re.sub(rules.CONTINUATION_WORDS_PATTERN, "", message, flags=re.I)
    return bool(
        any(re.fullmatch(pattern, message, re.I) for pattern in rules.STANDALONE_FOLLOWUP_PATTERNS)
        or (re.search(rules.CONTINUATION_WORDS_PATTERN, message, re.I)
            and not topic_terms(remainder.lower()))
        or (len(topic_terms(remainder.lower())) < rules.VAGUE_TOPIC_LIMIT
            and re.match(rules.QUALIFIED_CONTINUATION_PATTERN, message, re.I))
        or re.match(rules.COMMAND_REFERENCE_PATTERN, reference_message)
        or (len(topic_terms(message.lower())) < rules.VAGUE_TOPIC_LIMIT
            and re.search(rules.REFERENCE_PATTERN, reference_message))
    )
