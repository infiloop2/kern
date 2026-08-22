"""Contracts for bounded local embedding inference and its admin client."""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import MagicMock, patch

from host.runtime.embeddings import client, service


class EmbeddingClientTests(unittest.TestCase):
    def test_accepts_one_bounded_model_response(self) -> None:
        payload = json.dumps(
            {
                "model": client.MODEL_NAME,
                "embeddings": [[0.25] * client.MODEL_DIMENSIONS],
            }
        ).encode()
        response = MagicMock(status=200)
        response.getheader.return_value = str(len(payload))
        response.read.return_value = payload
        connection = MagicMock()
        connection.getresponse.return_value = response
        with patch.object(client, "_UnixHTTPConnection", return_value=connection):
            result = client.embed_texts(["sign-in problem"], kind="query")

        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0]), client.MODEL_DIMENSIONS)
        connection.request.assert_called_once()
        connection.close.assert_called_once()

    def test_rejects_oversized_input_and_wrong_vector_shape(self) -> None:
        with self.assertRaises(ValueError):
            client.embed_texts(["x" * (client.MAX_TEXT_BYTES + 1)], kind="passage")

        payload = json.dumps(
            {"model": client.MODEL_NAME, "embeddings": [[0.0, 1.0]]}
        ).encode()
        response = MagicMock(status=200)
        response.getheader.return_value = str(len(payload))
        response.read.return_value = payload
        connection = MagicMock()
        connection.getresponse.return_value = response
        with (
            patch.object(client, "_UnixHTTPConnection", return_value=connection),
            self.assertRaises(client.EmbeddingError),
        ):
            client.embed_texts(["hello"], kind="passage")

    def test_rejects_text_that_cannot_be_encoded_as_utf8(self) -> None:
        with self.assertRaisesRegex(ValueError, "valid UTF-8"):
            client.embed_texts(["\ud800"], kind="query")

    def test_only_http_400_marks_a_batch_as_rejected(self) -> None:
        for status, rejected in ((400, True), (503, False)):
            response = MagicMock(status=status)
            response.getheader.return_value = "2"
            response.read.return_value = b"{}"
            connection = MagicMock()
            connection.getresponse.return_value = response
            with (
                self.subTest(status=status),
                patch.object(client, "_UnixHTTPConnection", return_value=connection),
                self.assertRaises(client.EmbeddingError) as raised,
            ):
                client.embed_texts(["hello"], kind="passage")
            self.assertEqual(raised.exception.batch_rejected, rejected)

    def test_serializes_a_full_non_ascii_batch_within_the_service_limit(self) -> None:
        # A largest-legal batch of non-ASCII text must still fit the service's
        # request cap; escaped serialization would triple it and the indexer
        # would retry the same rejected pages forever.
        self._assert_batch_fits("🙂" * (client.MAX_TEXT_BYTES // len("🙂".encode())))

    def test_serializes_a_worst_case_escaped_batch_within_the_service_limit(self) -> None:
        # ensure_ascii=False still escapes these: a backslash doubles and a
        # control byte becomes six. A batch of valid texts must fit anyway, or
        # the service refuses it and the indexer reselects the same rows.
        for text in ("\\" * client.MAX_TEXT_BYTES, "\x01" * client.MAX_TEXT_BYTES):
            with self.subTest(sample=repr(text[:1])):
                self._assert_batch_fits(text)

    def _assert_batch_fits(self, text: str) -> None:
        texts = [text] * client.MAX_TEXTS
        payload = json.dumps(
            {"model": client.MODEL_NAME, "embeddings": [[0.25] * client.MODEL_DIMENSIONS] * client.MAX_TEXTS}
        ).encode()
        response = MagicMock(status=200)
        response.getheader.return_value = str(len(payload))
        response.read.return_value = payload
        connection = MagicMock()
        connection.getresponse.return_value = response
        with patch.object(client, "_UnixHTTPConnection", return_value=connection):
            client.embed_texts(texts, kind="passage")

        sent = connection.request.call_args.kwargs["body"]
        self.assertLessEqual(len(sent), service.MAX_REQUEST_BYTES)
        self.assertEqual(json.loads(sent)["texts"], texts)


class EmbeddingServiceTests(unittest.TestCase):
    def test_uses_distinct_query_and_passage_model_paths(self) -> None:
        model = MagicMock()
        model.query_embed.return_value = [[0.0] * client.MODEL_DIMENSIONS]
        model.passage_embed.return_value = [[1.0] * client.MODEL_DIMENSIONS]
        with patch.object(service, "_model", return_value=model):
            query = service.embed(["find it"], "query")
            passage = service.embed(["stored text"], "passage")

        self.assertEqual(query[0][0], 0.0)
        self.assertEqual(passage[0][0], 1.0)
        model.query_embed.assert_called_once_with(["find it"])
        model.passage_embed.assert_called_once_with(["stored text"])

    def test_accepts_only_admin_and_workspace_service_uids(self) -> None:
        admin = MagicMock(pw_uid=101)
        workspace = MagicMock(pw_uid=202)

        def user(name: str) -> MagicMock:
            return admin if name == "kern-admin" else workspace

        with patch.object(service.pwd, "getpwnam", side_effect=user):
            self.assertEqual(service._allowed_uids(), frozenset({101, 202}))

    def test_missing_service_accounts_do_not_widen_the_allowed_uids(self) -> None:
        """A host that lost an account gets a closed door, not this process's uid."""
        with patch.object(service.pwd, "getpwnam", side_effect=KeyError):
            with patch.dict(service.os.environ, {}, clear=True):
                self.assertEqual(service._allowed_uids(), frozenset())
            with patch.dict(
                service.os.environ, {"KERN_EMBEDDING_ALLOW_SELF_UID": "1"}, clear=True
            ):
                self.assertEqual(service._allowed_uids(), frozenset({os.getuid()}))

    def test_rejects_text_that_cannot_be_encoded_as_utf8(self) -> None:
        self.assertFalse(service._valid_texts(["\ud800"]))


if __name__ == "__main__":
    unittest.main()
